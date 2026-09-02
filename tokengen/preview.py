"""Vistas previas: PNG cenital (rápida, para el reporte) y HTML con three.js.

El HTML es autocontenido salvo por el CDN de three.js: se puede mandar por mail
a un cliente y abre en cualquier navegador. Es el mismo visor que va embebido en
la landing.
"""

from __future__ import annotations

import json

import numpy as np
from PIL import Image, ImageDraw

__all__ = ["render_topdown", "write_three_html"]


def render_topdown(stack, path: str, px_per_mm: float = 12.0, pad_mm: float = 2.0) -> str:
    """Render cenital plano de las capas, en orden de apilado."""
    ss = 3  # supersampling: se dibuja en grande y se reduce, para bordes suaves
    minx, miny, maxx, maxy = stack.bounds_mm
    scale = px_per_mm * ss
    w = max(int((maxx - minx + 2 * pad_mm) * scale), 64)
    h = max(int((maxy - miny + 2 * pad_mm) * scale), 64)
    canvas = Image.new("RGB", (w, h), (248, 248, 250))

    def tx(x, y):
        return ((x - minx + pad_mm) * scale, (maxy - y + pad_mm) * scale)

    # Cada capa se compone con su propia máscara (exterior menos huecos): así un
    # hueco deja ver la capa de abajo en vez de pintarse del color de fondo.
    for layer in sorted(stack.layers, key=lambda l: l.z1):
        mask = Image.new("L", (w, h), 0)
        md = ImageDraw.Draw(mask)
        drew = False
        for poly in getattr(layer.geometry, "geoms", [layer.geometry]):
            if poly.is_empty:
                continue
            md.polygon([tx(*p) for p in poly.exterior.coords], fill=255)
            for ring in poly.interiors:
                md.polygon([tx(*p) for p in ring.coords], fill=0)
            drew = True
        if drew:
            canvas.paste(Image.new("RGB", (w, h), tuple(int(c) for c in layer.rgb)), (0, 0), mask)

    canvas = canvas.resize((w // ss, h // ss), Image.LANCZOS)
    canvas.save(path)
    return path


def write_three_html(meshes, path: str, title: str = "TokenGen") -> str:
    """Visor 3D autocontenido: geometría embebida como JSON."""
    parts = []
    for layer, m in meshes:
        v = np.asarray(m.vertices, dtype=np.float32)
        f = np.asarray(m.faces, dtype=np.int32)
        parts.append(
            {
                "name": layer.name,
                "color": "#%02x%02x%02x" % tuple(int(c) for c in layer.rgb),
                "position": v.ravel().round(3).tolist(),
                "index": f.ravel().tolist(),
            }
        )

    data = json.dumps(parts, separators=(",", ":"))
    html = _TEMPLATE.replace("__TITLE__", title).replace("__DATA__", data)
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(html)
    return path


_TEMPLATE = """<!doctype html>
<html lang="es"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>__TITLE__ — TokenGen preview</title>
<style>
  html,body{margin:0;height:100%;background:#15171c;color:#e8e8ea;
    font:14px/1.5 ui-sans-serif,system-ui,-apple-system,Segoe UI,Roboto,sans-serif}
  #app{position:fixed;inset:0}
  #hud{position:fixed;left:16px;top:16px;padding:12px 14px;border-radius:10px;
    background:rgba(0,0,0,.55);backdrop-filter:blur(8px);max-width:260px}
  #hud h1{margin:0 0 8px;font-size:14px;letter-spacing:.02em}
  .row{display:flex;align-items:center;gap:8px;margin:4px 0;cursor:pointer;opacity:.95}
  .sw{width:13px;height:13px;border-radius:3px;box-shadow:0 0 0 1px rgba(255,255,255,.25)}
  .off{opacity:.3}
  #tip{position:fixed;right:16px;bottom:14px;opacity:.5;font-size:12px}
</style></head><body>
<div id="app"></div>
<div id="hud"><h1>__TITLE__</h1><div id="legend"></div></div>
<div id="tip">arrastrar: rotar · rueda: zoom · click en el color: ocultar</div>
<script type="importmap">
{"imports":{"three":"https://unpkg.com/three@0.160.0/build/three.module.js",
"three/addons/":"https://unpkg.com/three@0.160.0/examples/jsm/"}}
</script>
<script type="module">
import * as THREE from 'three';
import {OrbitControls} from 'three/addons/controls/OrbitControls.js';

const PARTS = __DATA__;
const app = document.getElementById('app');
const scene = new THREE.Scene();
scene.background = new THREE.Color(0x15171c);

const renderer = new THREE.WebGLRenderer({antialias:true});
renderer.setPixelRatio(Math.min(devicePixelRatio,2));
renderer.setSize(innerWidth, innerHeight);
app.appendChild(renderer.domElement);

const camera = new THREE.PerspectiveCamera(38, innerWidth/innerHeight, 0.1, 5000);
const controls = new OrbitControls(camera, renderer.domElement);
controls.enableDamping = true;

scene.add(new THREE.HemisphereLight(0xffffff, 0x33363d, 1.6));
const key = new THREE.DirectionalLight(0xffffff, 1.5); key.position.set(40,60,80); scene.add(key);
const fill = new THREE.DirectionalLight(0xffffff, .5); fill.position.set(-50,-30,20); scene.add(fill);

const group = new THREE.Group();
const legend = document.getElementById('legend');

for (const p of PARTS){
  const g = new THREE.BufferGeometry();
  g.setAttribute('position', new THREE.Float32BufferAttribute(p.position,3));
  g.setIndex(p.index);
  g.computeVertexNormals();
  const mat = new THREE.MeshStandardMaterial({color:p.color, roughness:.62, metalness:.02});
  const mesh = new THREE.Mesh(g, mat);
  group.add(mesh);

  const row = document.createElement('div');
  row.className = 'row';
  row.innerHTML = `<span class="sw" style="background:${p.color}"></span><span>${p.name}</span>`;
  row.onclick = () => { mesh.visible = !mesh.visible; row.classList.toggle('off', !mesh.visible); };
  legend.appendChild(row);
}
scene.add(group);

const box = new THREE.Box3().setFromObject(group);
const size = box.getSize(new THREE.Vector3());
const center = box.getCenter(new THREE.Vector3());
group.position.sub(center);
const r = Math.max(size.x, size.y, size.z);
camera.position.set(r*0.9, -r*1.5, r*1.3);
camera.up.set(0,0,1);
controls.target.set(0,0,0);

const grid = new THREE.GridHelper(Math.ceil(r*3), 20, 0x3a3f48, 0x272b31);
grid.rotation.x = Math.PI/2; grid.position.z = -size.z/2 - .01;
scene.add(grid);

addEventListener('resize', () => {
  camera.aspect = innerWidth/innerHeight; camera.updateProjectionMatrix();
  renderer.setSize(innerWidth, innerHeight);
});
(function loop(){ requestAnimationFrame(loop); controls.update(); renderer.render(scene,camera); })();
</script></body></html>
"""
