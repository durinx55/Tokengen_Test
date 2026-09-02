# TokenGen — Community Edition

**De una imagen a un 3MF multiparte listo para AMS/MMU, con validación de imprimibilidad antes de laminar.**

No es un conversor de imagen a STL. Es el núcleo abierto de un pipeline de
producción para print farms y tiendas que hoy diseñan **cada pieza a mano**:
llaveros, medallas, tokens de juego, chapitas personalizadas.

```bash
pip install -e .
python -m tokengen demo
```

```
  ████ TokenGen 0.3.0 · Community Edition
  imagen → 3MF multiparte, con validación de imprimibilidad

  1/6 raster        700×700 px → 3 colores
  2/6 vectorizado   vtracer: 13 contornos, 0.0643 mm/px
  3/6 unión acum.   3 capas; 0.988 mm² absorbidos, 0.915 mm² de solape
  4/6 validación    0 errores, 6 avisos
  5/6 malla         3 partes, 2884 triángulos, estanca=True, ≈1.72 g
  6/6 exportado     demo_input.3mf (+ report.json, preview)
```

<p align="center">
  <img src="docs/preview_inlay.png" width="300" alt="Salida en modo inlay">
  <img src="docs/preview_relief.png" width="300" alt="Salida en modo relief">
</p>

---

## El problema que resuelve

Una print farm que vende llaveros personalizados hace, por cada pedido, más o
menos esto:

1. Abre el logo del cliente en un editor vectorial y lo limpia a mano.
2. Lo importa a Fusion/Blender, lo extruye, separa los colores en cuerpos.
3. Exporta, lamina, descubre que un trazo era demasiado fino o que dos colores
   quedaron pegados.
4. Vuelve al paso 1.

Son **~1 hora de CAD por pieza**, y no escala: 40 pedidos son 40 horas. El
cuello de botella no es la impresora, es el diseño.

TokenGen reemplaza ese paso por un comando. Y —esto es lo que lo separa de los
conversores genéricos— **avisa antes de imprimir** qué se va a romper.

---

## El pipeline

```
  imagen.png
      │
      ▼
┌─────────────────────┐
│ 1. Pillow + numpy   │  recorte de fondo por flood-fill · cuantización anclada
│    raster.py        │  a los filamentos reales del AMS · despeckle
└─────────────────────┘
      │  mapa de índices (H,W) + paleta
      ▼
┌─────────────────────┐
│ 2. vtracer          │  una máscara binaria por color → contornos SVG
│    vectorize.py     │  parser propio · Bézier aplanadas con tolerancia en mm
└─────────────────────┘
      │  polígonos shapely, ya en milímetros
      ▼
┌─────────────────────┐
│ 3. UNIÓN ACUMULATIVA│  ◄── el núcleo del asunto
│    layers.py        │  partición exacta de la silueta · asignación de Z
└─────────────────────┘
      │  stack de capas de color
      ├──────────────────────────────┐
      ▼                              ▼
┌─────────────────────┐   ┌─────────────────────┐
│ 4. earcut + trimesh │   │ VALIDACIÓN          │
│    mesh.py          │   │ validate.py         │
│ un sólido por color │   │ 9 reglas físicas    │
└─────────────────────┘   └─────────────────────┘
      │                              │
      ▼                              ▼
┌─────────────────────┐        report.json
│ 5. 3MF multiparte   │        (imprimible: sí/no + por qué)
│    threemf.py       │
└─────────────────────┘
      │
      ├── token.3mf          ← se abre en Orca/Bambu Studio con los colores puestos
      ├── token.report.json  ← integrable en un flujo automatizado
      ├── token.preview.png
      └── token.preview.html ← visor three.js autocontenido, mandable por mail
```

---

## Paso 3: la unión acumulativa

Es el módulo que justifica que esto exista.

**El problema.** Cada color se traza por separado, desde su propia máscara. Dos
regiones que en la imagen eran vecinas píxel a píxel, al vectorizarse por
separado quedan con bordes que *casi* coinciden: sobran micro-solapes de
0,02 mm y faltan micro-huecos de 0,03 mm. En pantalla no se ven. En el slicer,
cada hueco es una pared de aire que rompe la capa, y cada solape es geometría
auto-intersectada que dispara *non-manifold*.

Un `unary_union` de todo junto no sirve: fusiona los colores y se pierde la
separación. Lo que hace falta es una **partición**.

**La solución.** Una pasada acumulativa en orden de prioridad, donde cada color
reclama únicamente lo que ningún color anterior reclamó:

```python
reclamado = ∅
for c in orden_de_prioridad:            # de menor a mayor área
    region[c] = geom[c] − reclamado     # el detalle fino gana sobre el fondo
    reclamado = reclamado ∪ region[c]

resto = silueta − reclamado             # los micro-huecos
region[base] ∪= resto                   # los absorbe el color de fondo
```

Sin solapes por construcción. Sin huecos, porque el resto se reabsorbe. La suma
de las regiones es idénticamente la silueta. En la pieza de demo eso son
**0,988 mm² de micro-huecos** y **0,915 mm² de solapes** que jamás llegan al
slicer, y que a mano habrías cazado recién en la tercera iteración.

### Y una segunda vez, para el modo relieve

En `--mode relief` la unión acumulativa se aplica otra vez, ahora de arriba
hacia abajo: la banda `j` abarca su color **y todos los de más arriba**.

```
cum[j] = ∪ region[k]  para todo k ≥ j
```

Como cada banda contiene a la de encima, sale gratis que:

* cada capa apoya al 100 % sobre la de abajo — **cero voladizos**;
* cada franja de Z es de **un solo filamento** — un cambio por banda, purga mínima;
* el color visible en cada punto es el de la banda más alta que lo cubre, que es
  exactamente el color original.

| `--mode inlay` (por defecto) | `--mode relief` |
|---|---|
| Mosaico coplanar: placa base + una banda de color | Escalonado: una franja de Z por color |
| Pieza más baja, lectura frontal nítida | Relieve táctil, cambios de filamento mínimos |
| Más cambios de herramienta por capa | Sin voladizos por construcción |

---

## Las validaciones

Se corren sobre geometría 2D en milímetros **antes** de generar la malla, que es
cuando corregir todavía sale barato. Cada regla responde a un modo de falla real:

| Regla | Falla que evita |
|---|---|
| `thin_features` | Detalle más fino que un perímetro: el slicer lo omite **en silencio** |
| `micro_gaps` | Separación sub-boquilla: dos colores se funden y se pierde el borde |
| `micro_holes` | Agujero que el slicer tapa al no poder trazar la pared interna |
| `tiny_islands` | Islas sueltas que se despegan de la cama o que el AMS arrastra |
| `stacking_order` | Color que arranca sin material debajo (imposible de apilar) |
| `overhangs` | Alero que excede lo puenteable sin soporte |
| `layer_quantization` | Banda de color que no es múltiplo de la altura de capa |
| `keyring` | Agujero de llavero con pared demasiado fina al borde |
| `size_sanity` | Pieza fuera de rango útil o fuera de la cama |

La imagen de demo tiene los cuatro primeros defectos **plantados a propósito**.
Salida real:

```
 ! thin_features [AMS3_Red]: detalle por debajo de 0.42 mm (15.7% del color visible)
     → Escalá ×2.2 o simplificá el arte.
 ! micro_holes [AMS4_Yellow]: huecos interiores por debajo de ⌀0.8 mm
     → Agrandalos, o eliminalos a propósito para que el resultado sea predecible.
 ! tiny_islands [AMS3_Red]: islas menores a 1.0 mm²; riesgo de arrastre por el AMS
     → Subí --despeckle o aumentá el tamaño de la pieza.
 ! micro_gaps: separación de 0.114 mm entre AMS4_Yellow y AMS3_Red (mínimo 0.45 mm)
     → Los colores se van a tocar. Aplicá --bleed negativo o unificalos.

 Veredicto: IMPRIMIBLE
```

Dos detalles de diseño que importan:

* Las reglas se evalúan sobre la **región visible** de cada color, no sobre el
  sólido. En modo relieve el sólido de un color es enorme (contiene a todos los
  de arriba) pero lo que el ojo ve puede ser un aro de 0,1 mm. Validar el sólido
  no detectaría nada.
* Cada aviso trae una **acción concreta**, con el número. `Escalá ×2.2`, no
  "revisá el modelo".

`tokengen check` valida sin exportar y devuelve código de salida ≠ 0 si la pieza
no es imprimible: se enchufa directo en un pipeline de pedidos.

---

## Por qué 3MF y no STL

Un STL es un sólido sin color. No puede representar nada de lo que hace falta acá.

El exportador está escrito a mano (zipfile + XML, sin dependencias) porque lo que
se necesita es específico:

* **Un `<object>` por color**, no una malla fusionada — es la única forma de que
  Orca/Bambu/PrusaSlicer permitan asignar filamento por parte.
* Un objeto de ensamblado con `<components>` que referencia a los demás, para que
  el archivo abra como **una sola pieza multiparte** y no como N piezas sueltas
  que hay que alinear a mano.
* `<basematerials>` con el color real de cada filamento, así la vista previa del
  slicer ya sale en colores.
* Nombres de parte con el slot del AMS (`AMS3_Red`), para no adivinar al cargar.

---

## Uso

```bash
# llavero de 45 mm, 3 colores, con agujero, sobre paleta Bambu PLA Basic
python -m tokengen build logo.png --width-mm 45 --colors 3 --keyring --stock-palette

# relieve, boquilla fina
python -m tokengen build arte.jpg --mode relief --profile 0.4-pla-fino -o salida/

# sólo validar (exit code 1 si no es imprimible)
python -m tokengen check logo.png --width-mm 20

# salida JSON para automatizar
python -m tokengen build logo.png --json

# perfiles disponibles
python -m tokengen profiles
```

### Paleta propia

Los colores no se eligen: se **anclan a los filamentos que tenés cargados**. De
nada sirve un modelo de 12 colores si el AMS tiene 4 slots.

```bash
python -m tokengen build logo.png --palette mis_filamentos.json
```

```json
[
  {"name": "Matte Charcoal", "rgb": [40, 40, 44], "slot": 1},
  {"name": "Bambu Jade White", "rgb": [245, 245, 245], "slot": 2},
  {"name": "Sunflower", "rgb": [243, 195, 42], "slot": 3}
]
```

### Parámetros principales

| Flag | Por defecto | Para qué |
|---|---|---|
| `--width-mm` | 50 | Ancho final de la pieza |
| `--colors` | 3 | Cantidad de filamentos (máx. 4 en Community) |
| `--mode` | `inlay` | `inlay` o `relief` |
| `--base-mm` / `--color-mm` | 0.8 / 0.6 | Espesor de la base y de cada banda |
| `--profile` | `0.4-pla-ams` | Perfil físico: de acá salen los umbrales |
| `--keyring` | — | Perfora el agujero buscando una posición con pared suficiente |
| `--bleed-mm` | 0 | Sangrado por color, para compensar el flujo |
| `--fillet-mm` | 0 | Redondeo del contorno externo |
| `--despeckle` | 6 | Mancha mínima en px, antes de vectorizar |

---

## Instalación

```bash
git clone https://github.com/TU_USUARIO/tokengen
cd tokengen
pip install -e .
python -m tokengen demo
```

Requiere Python ≥ 3.10. `vtracer` es opcional: sin él, el pipeline cae en un
trazador de respaldo en numpy/shapely puro (más lento y con contornos más
gruesos, pero funciona — útil en CI o sin toolchain de Rust).

---

## Notas de implementación

Tres cosas que aparecieron probando contra piezas reales y que valen para
cualquiera que arme algo parecido:

1. **`trimesh.merge_vertices()` con la tolerancia por defecto rompe el detalle
   fino.** Trimesh la deriva de la escala de la malla; en una pieza de 45 mm
   alcanza para colapsar el anillo de un hueco de 0,4 mm². La malla queda
   no-manifold justo en lo que más importa. `extrude_polygon` ya devuelve
   índices correctos: no hay que soldar nada.

2. **Quitarle caras degeneradas a una malla ya estanca le abre agujeros.** La
   limpieza tiene que ser condicional: primero verificar `is_watertight`, y sólo
   intervenir si falla.

3. **Los polígonos que se auto-tocan en un punto rompen earcut.** Es el
   resultado típico de un `difference` con tolerancia. Un `buffer(0)` previo
   separa el pinzamiento en polígonos independientes y la triangulación vuelve a
   ser manifold.

Cada malla exportada se verifica estanca antes de escribir el 3MF.

---

## Community Edition vs. completa

Esto es el **núcleo abierto**: el pipeline entero, funcionando, sin recortes en
la geometría ni en las validaciones. Lo que verificás acá es lo que corre en
producción.

| | Community (este repo) | Completa |
|---|---|---|
| Pipeline imagen → 3MF | ✅ | ✅ |
| Unión acumulativa, ambos modos | ✅ | ✅ |
| Las 9 validaciones | ✅ | ✅ |
| Perfiles y paletas propias | ✅ | ✅ |
| Cantidad de colores | hasta 4 | sin tope |
| Procesamiento por lotes de pedidos | — | ✅ |
| Optimización de torre de purga y orden de herramienta | — | ✅ |
| Configurador web paramétrico (Shopify / WooCommerce / Wix) | — | ✅ |
| API HTTP y webhooks para integrar al flujo de pedidos | — | ✅ |
| Nesting en cama y estimación de costo por pieza | — | ✅ |

El tope de 4 colores no es arbitrario: es lo que entra en un AMS. Para la
mayoría de los llaveros y medallas, Community alcanza y sobra.

---

## Para quién

Si tenés una print farm, una tienda de personalizados o un software de
configuración de productos y hoy alguien diseña cada pieza a mano, esto
reemplaza ese paso. El pipeline se adapta a tu catálogo, tu paleta y tu slicer.

**Ariel Ferrari** — desarrollo de pipelines de automatización de geometría 3D
· [pipeline@ejemplo.com](mailto:pipeline@ejemplo.com)

---

## Licencia

MIT. Ver [LICENSE](LICENSE).

---

## Tests

```bash
pip install -e ".[dev]"
pytest -q
```

18 tests sobre los **invariantes**, no sobre valores exactos: que las regiones
visibles sumen la silueta sin huecos ni solapes, que las bandas de relieve estén
anidadas, que cada sólido salga estanco, que el 3MF recargue con las partes
nombradas, y que el validador dispare ante cada defecto plantado en la imagen de
ejemplo.

El CI corre la matriz completa **con y sin vtracer**: el trazador de respaldo
tiene que sostener el pipeline entero cuando no hay toolchain de Rust.
