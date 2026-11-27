"""Writes the two models the public KiCad library does not have (VRML 2.0,
KiCad units: 1 = 0.1 inch). Generated, so they can be re-made and changed."""
import math, sys
U = 1 / 2.54  # mm -> VRML units
OUT = sys.argv[1]

def mesh(verts, faces, color, shininess=0.3, spec=0.3):
    v = ", ".join(f"{x*U:.4f} {y*U:.4f} {z*U:.4f}" for x, y, z in verts)
    f = ", ".join(" ".join(map(str, fc)) + " -1" for fc in faces)
    r, g, b = color
    return f"""Shape {{ appearance Appearance {{ material Material {{ diffuseColor {r} {g} {b} specularColor {spec} {spec} {spec} shininess {shininess} }} }}
  geometry IndexedFaceSet {{ solid FALSE creaseAngle 0.6 coord Coordinate {{ point [ {v} ] }} coordIndex [ {f} ] }} }}
"""

def box(x0, y0, z0, x1, y1, z1, color, **kw):
    V = [(x, y, z) for z in (z0, z1) for y in (y0, y1) for x in (x0, x1)]
    F = [(0, 2, 3, 1), (4, 5, 7, 6), (0, 1, 5, 4), (2, 6, 7, 3), (0, 4, 6, 2), (1, 3, 7, 5)]
    return mesh(V, F, color, **kw)

def toroid():
    """Vertical wound toroid on a header base: 16 mm OD, 7 mm ID, 8.5 mm thick
    ferrite core with copper turns, standing in a black plastic base whose two
    leads go to the footprint pads at y = +-2.5 mm. Ring in the x-z plane."""
    R, a, b = 5.75, 2.25, 4.25        # core centre radius, radial / axial half widths
    base_h = 2.0
    w = 0.42                          # wire radius
    zc = base_h + R + a + w           # core centre height

    def section(s, ea, eb, n=4.0):     # rounded-rectangle (superellipse) cross section
        c, d = math.cos(s), math.sin(s)
        return (ea * math.copysign(abs(c) ** (2 / n), c), eb * math.copysign(abs(d) ** (2 / n), d))

    def ring_point(t, x, y):          # x: radial offset, y: axial (thickness, along y)
        r = R + x
        return (r * math.cos(t), y, zc + r * math.sin(t))

    out = ""
    # core
    n, m = 96, 32
    V, F = [], []
    for i in range(n):
        t = 2 * math.pi * i / n
        for j in range(m):
            x, y = section(2 * math.pi * j / m, a, b)
            V.append(ring_point(t, x, y))
    p = lambda ii, jj: (ii % n) * m + (jj % m)
    for i in range(n):
        for j in range(m):
            F.append((p(i, j), p(i, j + 1), p(i + 1, j + 1), p(i + 1, j)))
    out += mesh(V, F, (0.16, 0.17, 0.16), shininess=0.2, spec=0.15)

    # winding: turns spaced evenly, each a tube following the core section
    turns, ps, cs = 26, 40, 8
    for k in range(turns):
        t = 2 * math.pi * (k + 0.5) / turns
        if abs(math.sin(t) + 1) < 0.08:   # leave the bottom free where the base clamps
            continue
        path = []
        for j in range(ps):
            x, y = section(2 * math.pi * j / ps, a + w, b + w)
            path.append((x, y))
        V, F = [], []
        for j in range(ps):
            x0, y0 = path[j]; x1, y1 = path[(j + 1) % ps]
            tx, ty = x1 - x0, y1 - y0; L = math.hypot(tx, ty) or 1
            nx, ny = ty / L, -tx / L        # in-plane normal
            for q in range(cs):
                ph = 2 * math.pi * q / cs
                rx = x0 + w * math.cos(ph) * nx
                ry = y0 + w * math.cos(ph) * ny
                along = w * math.sin(ph)    # along the ring direction
                X, Y, Z = ring_point(t, rx, ry)
                V.append((X - along * math.sin(t), Y, Z + along * math.cos(t)))
        pp = lambda jj, qq: (jj % ps) * cs + (qq % cs)
        for j in range(ps):
            for q in range(cs):
                F.append((pp(j, q), pp(j + 1, q), pp(j + 1, q + 1), pp(j, q + 1)))
        out += mesh(V, F, (0.80, 0.45, 0.20), shininess=0.7, spec=0.6)

    # header base and leads
    out += box(-5.5, -4.6, 0.0, 5.5, 4.6, base_h, (0.06, 0.06, 0.06), shininess=0.3)
    out += box(-3.0, -4.6, base_h, 3.0, 4.6, base_h + 1.2, (0.06, 0.06, 0.06), shininess=0.3)
    for y in (-2.5, 2.5):
        out += box(-0.35, y - 0.35, -3.0, 0.35, y + 0.35, 0.2, (0.85, 0.85, 0.85), shininess=0.8, spec=0.7)
    return out


def devkit():
    """30-pin ESP32 DevKit (DOIT V1 size) seen from above: board, header strips
    underneath, pin stubs on top. Footprint origin = centre between the inner rows."""
    out = ""
    z0 = 8.5 + 2.5                     # socket height + header plastic
    out += box(-25.75, -14.15, z0, 25.75, 14.15, z0 + 1.6, (0.05, 0.05, 0.08), shininess=0.2)
    for y in (-12.7, 12.7):
        out += box(-19.05, y - 1.27, 8.5, 19.05, y + 1.27, z0, (0.08, 0.08, 0.08))
        for k in range(15):
            x = -17.78 + 2.54 * k
            out += box(x - 0.32, y - 0.32, z0 + 1.6, x + 0.32, y + 0.32, z0 + 3.6, (0.85, 0.7, 0.3), shininess=0.8, spec=0.7)
    for x in (-21.5, -21.5):           # EN / BOOT buttons near USB
        pass
    for y in (-8.5, 8.5):
        out += box(-22.5, y - 1.7, z0 + 1.6, -19.5, y + 1.7, z0 + 3.1, (0.75, 0.75, 0.75))
        out += box(-21.6, y - 0.75, z0 + 3.1, -20.4, y + 0.75, z0 + 3.8, (0.1, 0.1, 0.1))
    return out

for name, body in (("Toroid_Vertical_D16.0mm_T8.5mm_P5.00mm", toroid()), ("ESP32_DevKit_30pin_DOIT_V1", devkit())):
    open(f"{OUT}/{name}.wrl", "w").write("#VRML V2.0 utf8\n# generated by custom_models.py (flipdot-display)\n" + body)
    print("wrote", name)
