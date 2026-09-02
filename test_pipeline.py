"""Tests del pipeline.

Se concentran en los **invariantes**, no en valores exactos: la partición de la
silueta, la estanqueidad de cada sólido, el anidamiento de bandas en relieve y
que el validador efectivamente dispare ante defectos plantados.
"""

from __future__ import annotations

import sys
import zipfile
from pathlib import Path

import pytest
from shapely.ops import unary_union

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from examples.make_sample import make_sample  # noqa: E402
from tokengen.config import DEFAULT_PALETTE, PROFILES, TokenSpec  # noqa: E402
from tokengen.pipeline import run  # noqa: E402


@pytest.fixture(scope="session")
def sample(tmp_path_factory) -> str:
    return make_sample(str(tmp_path_factory.mktemp("in") / "sample.png"))


def _run(sample, tmp_path, **kw):
    kw.setdefault("width_mm", 45)
    spec = TokenSpec(colors=3, palette=DEFAULT_PALETTE, **kw)
    return run(sample, str(tmp_path), spec, write_preview=False, on_log=lambda *a: None)


# ------------------------------------------------- unión acumulativa --------


def test_particion_exacta_de_la_silueta(sample, tmp_path):
    """Las regiones visibles suman la silueta: ni huecos ni solapes."""
    r = _run(sample, tmp_path)
    union = unary_union([l.visible for l in r.stack.layers])

    faltante = r.stack.silhouette.difference(union).area
    sobrante = union.difference(r.stack.silhouette).area
    assert faltante < 1e-6, f"quedaron {faltante} mm² sin cubrir"
    assert sobrante < 1e-6, f"sobran {sobrante} mm² fuera de la silueta"


def test_regiones_visibles_no_se_solapan(sample, tmp_path):
    r = _run(sample, tmp_path)
    layers = r.stack.layers
    for i, a in enumerate(layers):
        for b in layers[i + 1 :]:
            assert a.visible.intersection(b.visible).area < 1e-6


def test_relieve_las_bandas_estan_anidadas(sample, tmp_path):
    """cum[j+1] ⊆ cum[j]: apoyo total y cero voladizos por construcción."""
    r = _run(sample, tmp_path, mode="relief")
    ordenadas = sorted(r.stack.layers, key=lambda l: l.z1)
    for abajo, arriba in zip(ordenadas, ordenadas[1:]):
        fuera = arriba.geometry.difference(abajo.geometry.buffer(1e-9)).area
        assert fuera < 1e-6, f"{arriba.name} sobresale {fuera} mm² de {abajo.name}"


def test_las_bandas_son_contiguas_en_z(sample, tmp_path):
    r = _run(sample, tmp_path, mode="relief")
    ordenadas = sorted(r.stack.layers, key=lambda l: l.z1)
    assert ordenadas[0].z0 == 0.0
    for abajo, arriba in zip(ordenadas, ordenadas[1:]):
        assert arriba.z0 == pytest.approx(abajo.z1)


# --------------------------------------------------------------- malla ------


@pytest.mark.parametrize("mode", ["inlay", "relief"])
def test_todos_los_solidos_son_estancos(sample, tmp_path, mode):
    r = _run(sample, tmp_path, mode=mode)
    assert r.meshes
    for layer, mesh in r.meshes:
        assert mesh.is_watertight, f"{layer.name} no es estanca"
        assert mesh.is_winding_consistent
        assert mesh.volume > 0


def test_la_pieza_respeta_el_ancho_pedido(sample, tmp_path):
    r = _run(sample, tmp_path)
    minx, miny, maxx, maxy = r.stack.bounds_mm
    assert maxx - minx <= 45.0 + 1e-6


# ----------------------------------------------------------------- 3MF ------


def test_el_3mf_es_multiparte_y_valido(sample, tmp_path):
    from xml.dom.minidom import parseString

    r = _run(sample, tmp_path)
    with zipfile.ZipFile(r.outputs["3mf"]) as z:
        assert "3D/3dmodel.model" in z.namelist()
        assert "[Content_Types].xml" in z.namelist()
        xml = z.read("3D/3dmodel.model").decode()

    parseString(xml)  # lanza si el XML es inválido
    assert xml.count("<object ") == len(r.meshes) + 1  # + el ensamblado
    assert "<basematerials" in xml
    assert "<components>" in xml


def test_el_3mf_recarga_con_las_partes_nombradas(sample, tmp_path):
    import trimesh

    r = _run(sample, tmp_path)
    escena = trimesh.load(r.outputs["3mf"])
    assert len(escena.geometry) == len(r.meshes)
    for layer, _ in r.meshes:
        assert layer.name in escena.geometry


# ---------------------------------------------------------- validación ------


def test_detecta_los_defectos_plantados(sample, tmp_path):
    """La imagen de ejemplo trae cuatro defectos a propósito."""
    r = _run(sample, tmp_path)
    codigos = {i.code for i in r.report.issues}
    for esperado in ("thin_features", "micro_holes", "tiny_islands", "micro_gaps"):
        assert esperado in codigos, f"no se detectó {esperado}"


def test_altura_de_color_menor_a_una_capa_es_error(sample, tmp_path):
    r = _run(sample, tmp_path, color_mm=0.05)
    assert not r.report.printable
    assert any(i.code == "layer_quantization" for i in r.report.errors)


def test_pieza_diminuta_pierde_todo_el_detalle(sample, tmp_path):
    r = _run(sample, tmp_path.joinpath("mini"), width_mm=6)
    assert any(i.code in ("thin_features", "size_sanity") for i in r.report.issues)


def test_una_isla_dentro_de_un_hueco_sobrevive(sample, tmp_path):
    """Regresión: el disco central de un anillo no debe perderse.

    Los anillos trazados se anidan por paridad de profundidad. Si se unieran
    todos los sólidos y recién después se restaran los huecos, el hueco del
    anillo se comería el disco que tiene adentro — y la silueta quedaría con un
    agujero pasante donde debería haber material.
    """
    from shapely.geometry import Polygon

    r = _run(sample, tmp_path, keyring=(3.5, 2.0))
    huecos = [
        Polygon(anillo).area
        for poly in r.stack.silhouette.geoms
        for anillo in poly.interiors
    ]
    # El único agujero pasante legítimo es el del llavero (⌀3,5 ≈ 9,6 mm²).
    assert len(huecos) == 1, f"agujeros inesperados en la silueta: {huecos}"
    assert huecos[0] == pytest.approx(9.6, abs=1.0)


def test_el_reporte_serializa_a_json(sample, tmp_path):
    import json

    r = _run(sample, tmp_path)
    json.dumps(r.report.to_dict())  # no debe lanzar
    assert Path(r.outputs["report"]).exists()


# ------------------------------------------------------------- opciones -----


def test_el_llavero_perfora_un_agujero(sample, tmp_path):
    con = _run(sample, tmp_path / "a", keyring=(3.5, 2.0))
    sin = _run(sample, tmp_path / "b")
    assert con.stack.silhouette.area < sin.stack.silhouette.area


def test_la_paleta_asigna_slots_de_ams(sample, tmp_path):
    r = _run(sample, tmp_path)
    assert all(l.filament is not None for l in r.stack.layers)
    assert all(l.name.startswith("AMS") for l in r.stack.layers)


def test_community_limita_la_cantidad_de_colores(sample, tmp_path):
    from tokengen.config import COMMUNITY_MAX_COLORS

    spec = TokenSpec(width_mm=45, colors=12)
    r = run(sample, str(tmp_path), spec, write_preview=False, on_log=lambda *a: None)
    assert len(r.stack.layers) <= COMMUNITY_MAX_COLORS


def test_los_perfiles_cambian_los_umbrales(sample, tmp_path):
    fino = _run(sample, tmp_path / "f", profile=PROFILES["0.4-pla-fino"])
    grueso = _run(sample, tmp_path / "g", profile=PROFILES["0.6-pla-rapido"])
    assert len(grueso.report.issues) >= len(fino.report.issues)
