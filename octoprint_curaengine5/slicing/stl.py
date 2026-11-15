# SPDX-License-Identifier: AGPL-3.0-or-later
"""Reading of an STL (binary or ASCII) and translated rewriting, in pure Python.

On the command line, CuraEngine itself shifts the model by
(machine_width/2, machine_depth/2) when machine_center_is_zero is false: so we
center the model on the origin in X/Y and place it at Z=0.
"""
import math
import struct

_HEADER = 80
_TRIANGLE = struct.Struct("<12fH")


class StlError(Exception):
    pass


def _is_binary(data):
    if len(data) < _HEADER + 4:
        return False
    count = struct.unpack_from("<I", data, _HEADER)[0]
    return _HEADER + 4 + count * _TRIANGLE.size == len(data)


def read_triangles(path):
    """Returns the flat list of vertices [(x, y, z), ...] per triangle (3 vertices each)."""
    with open(path, "rb") as f:
        data = f.read()
    if _is_binary(data):
        vertices = []
        for values in _TRIANGLE.iter_unpack(memoryview(data)[_HEADER + 4:]):
            vertices.append(values[3:6])
            vertices.append(values[6:9])
            vertices.append(values[9:12])
        return vertices
    try:
        text = data.decode("ascii", errors="strict")
    except UnicodeDecodeError:
        raise StlError("Unreadable STL file (neither valid binary nor ASCII)")
    vertices = []
    for line in text.splitlines():
        parts = line.split()
        if len(parts) == 4 and parts[0].lower() == "vertex":
            try:
                vertices.append((float(parts[1]), float(parts[2]), float(parts[3])))
            except ValueError:
                raise StlError("Invalid vertex: {}".format(line.strip()))
    if not vertices or len(vertices) % 3:
        raise StlError("No triangle found in the STL")
    return vertices


def bounding_box(vertices):
    xs = [v[0] for v in vertices]
    ys = [v[1] for v in vertices]
    zs = [v[2] for v in vertices]
    return (min(xs), min(ys), min(zs)), (max(xs), max(ys), max(zs))


def prepare_for_engine(source, destination, offset_x=0.0, offset_y=0.0):
    """Writes to ``destination`` a binary STL centered in X/Y (+ offset) and placed at Z=0.

    :return: dimensions (dx, dy, dz) and number of triangles.
    """
    vertices = read_triangles(source)
    if not vertices:
        raise StlError("Empty STL")
    (minx, miny, minz), (maxx, maxy, maxz) = bounding_box(vertices)
    tx = -(minx + maxx) / 2.0 + offset_x
    ty = -(miny + maxy) / 2.0 + offset_y
    tz = -minz
    count = len(vertices) // 3
    with open(destination, "wb") as f:
        f.write(b"OctoPrint-CuraEngine5".ljust(_HEADER, b" "))
        f.write(struct.pack("<I", count))
        pack = _TRIANGLE.pack
        for i in range(0, len(vertices), 3):
            a, b, c = vertices[i], vertices[i + 1], vertices[i + 2]
            f.write(pack(0.0, 0.0, 0.0,
                         a[0] + tx, a[1] + ty, a[2] + tz,
                         b[0] + tx, b[1] + ty, b[2] + tz,
                         c[0] + tx, c[1] + ty, c[2] + tz, 0))
    return (maxx - minx, maxy - miny, maxz - minz), count


def write_box(path, sx, sy, sz):
    """Writes a box (useful for tests)."""
    v = [(x, y, z) for z in (0, sz) for y in (0, sy) for x in (0, sx)]
    faces = [(0, 2, 1), (1, 2, 3), (4, 5, 6), (5, 7, 6), (0, 1, 4), (1, 5, 4),
             (2, 6, 3), (3, 6, 7), (0, 4, 2), (2, 4, 6), (1, 3, 5), (3, 7, 5)]
    with open(path, "wb") as f:
        f.write(b"box".ljust(_HEADER, b" "))
        f.write(struct.pack("<I", len(faces)))
        for a, b, c in faces:
            f.write(_TRIANGLE.pack(0, 0, 0, *v[a], *v[b], *v[c], 0))


def _rotation_matrix(rx, ry, rz):
    """Rx·Ry·Rz matrix (angles in degrees), three.js's "XYZ" Euler order."""
    ax, ay, az = (math.radians(a) for a in (rx, ry, rz))
    cx, sx, cy, sy, cz, sz = math.cos(ax), math.sin(ax), math.cos(ay), math.sin(ay), math.cos(az), math.sin(az)
    return [
        [cy * cz, -cy * sz, sy],
        [cx * sz + sx * sy * cz, cx * cz - sx * sy * sz, -sx * cy],
        [sx * sz - cx * sy * cz, sx * cz + cx * sy * sz, cx * cy],
    ]


def transform_model(source, destination, rotation=(0, 0, 0), scale=(100, 100, 100)):
    """Writes an STL transformed around the center of the original bounding box.

    Each vertex becomes ``R · S · (v − c0)`` (scale in %, rotation in degrees): the model stays
    centered on the pivot origin; slicing will then place it on the bed.

    :return: X/Y center of the result's bounding box, relative to the pivot.
    """
    vertices = read_triangles(source)
    if not vertices:
        raise StlError("Empty STL")
    (minx, miny, minz), (maxx, maxy, maxz) = bounding_box(vertices)
    c0 = ((minx + maxx) / 2.0, (miny + maxy) / 2.0, (minz + maxz) / 2.0)
    m = _rotation_matrix(*rotation)
    sx, sy, sz = (float(v) / 100.0 for v in scale)
    out = []
    for x, y, z in vertices:
        x, y, z = (x - c0[0]) * sx, (y - c0[1]) * sy, (z - c0[2]) * sz
        out.append((m[0][0] * x + m[0][1] * y + m[0][2] * z,
                    m[1][0] * x + m[1][1] * y + m[1][2] * z,
                    m[2][0] * x + m[2][1] * y + m[2][2] * z))
    (lx, ly, _lz), (hx, hy, _hz) = bounding_box(out)
    with open(destination, "wb") as f:
        f.write(b"OctoPrint-CuraEngine5".ljust(_HEADER, b" "))
        f.write(struct.pack("<I", len(out) // 3))
        for i in range(0, len(out), 3):
            a, b, c = out[i], out[i + 1], out[i + 2]
            f.write(_TRIANGLE.pack(0.0, 0.0, 0.0, *a, *b, *c, 0))
    return (lx + hx) / 2.0, (ly + hy) / 2.0
