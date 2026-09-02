"""Etapa 5 — 3MF multiparte.

Se escribe a mano (zipfile + XML) en vez de usar un exportador genérico porque
lo que necesitamos es específico:

* **Un objeto por color**, no una malla fusionada. Es la única forma de que
  Orca/Bambu/PrusaSlicer permitan asignar filamento por parte.
* Un **objeto de ensamblado** con `<components>` que referencia a los demás, para
  que al abrir el archivo aparezca como una sola pieza multiparte y no como N
  piezas sueltas que hay que alinear a mano.
* `<basematerials>` con el color real de cada filamento, de modo que la vista
  previa del slicer ya salga en colores.

Un STL no puede representar nada de esto: es un solo sólido sin color. Por eso
el entregable es 3MF y no STL.
"""

from __future__ import annotations

import zipfile
from datetime import datetime, timezone
from xml.sax.saxutils import escape

import numpy as np

__all__ = ["write_3mf"]

CONTENT_TYPES = """<?xml version="1.0" encoding="UTF-8"?>
<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">
 <Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>
 <Default Extension="model" ContentType="application/vnd.ms-package.3dmanufacturing-3dmodel+xml"/>
 <Default Extension="png" ContentType="image/png"/>
</Types>
"""

RELS = """<?xml version="1.0" encoding="UTF-8"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
 <Relationship Target="/3D/3dmodel.model" Id="rel-1" Type="http://schemas.microsoft.com/3dmanufacturing/2013/01/3dmodel"/>
</Relationships>
"""

NS = "http://schemas.microsoft.com/3dmanufacturing/core/2015/02"


def write_3mf(path: str, parts, metadata: dict | None = None) -> str:
    """Escribe un 3MF multiparte.

    Parameters
    ----------
    path : destino .3mf
    parts : iterable de (nombre, mesh_trimesh, rgb)
    metadata : pares clave/valor que se embeben en el modelo
    """
    parts = list(parts)
    if not parts:
        raise ValueError("No hay partes para exportar.")

    xml = _build_model(parts, metadata or {})

    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED, compresslevel=6) as z:
        z.writestr("[Content_Types].xml", CONTENT_TYPES)
        z.writestr("_rels/.rels", RELS)
        z.writestr("3D/3dmodel.model", xml)
    return path


def _build_model(parts, metadata: dict) -> str:
    out: list[str] = []
    a = out.append

    a('<?xml version="1.0" encoding="UTF-8"?>')
    a(f'<model unit="millimeter" xml:lang="en-US" xmlns="{NS}">')

    meta = {
        "Application": "TokenGen Community Edition",
        "CreationDate": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "Designer": "TokenGen pipeline",
        **metadata,
    }
    for k, v in meta.items():
        a(f'  <metadata name="{escape(str(k))}">{escape(str(v))}</metadata>')

    a("  <resources>")

    # --- materiales: uno por parte, en el mismo orden ---
    a('    <basematerials id="1">')
    for name, _mesh, rgb in parts:
        color = "#%02X%02X%02XFF" % tuple(int(c) for c in rgb)
        a(f'      <base name="{escape(str(name))}" displaycolor="{color}"/>')
    a("    </basematerials>")

    # --- un objeto por color ---
    obj_ids = []
    for i, (name, mesh, _rgb) in enumerate(parts):
        oid = i + 2  # el id 1 lo ocupan los materiales
        obj_ids.append(oid)
        a(
            f'    <object id="{oid}" type="model" name="{escape(str(name))}" '
            f'pid="1" pindex="{i}">'
        )
        a("      <mesh>")
        a("        <vertices>")
        for x, y, z in np.asarray(mesh.vertices, dtype=float):
            a(f'          <vertex x="{x:.4f}" y="{y:.4f}" z="{z:.4f}"/>')
        a("        </vertices>")
        a("        <triangles>")
        for v1, v2, v3 in np.asarray(mesh.faces, dtype=int):
            a(f'          <triangle v1="{v1}" v2="{v2}" v3="{v3}"/>')
        a("        </triangles>")
        a("      </mesh>")
        a("    </object>")

    # --- ensamblado: agrupa todo en una sola pieza multiparte ---
    asm_id = obj_ids[-1] + 1
    a(f'    <object id="{asm_id}" type="model" name="TokenGen_assembly">')
    a("      <components>")
    for oid in obj_ids:
        a(f'        <component objectid="{oid}"/>')
    a("      </components>")
    a("    </object>")
    a("  </resources>")

    a("  <build>")
    a(f'    <item objectid="{asm_id}"/>')
    a("  </build>")
    a("</model>")
    return "\n".join(out)
