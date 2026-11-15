/*
 * OctoPrint-CuraEngine5 — three.js scene of the Slicer tab (ES module, loaded on demand).
 * three.js (MIT) is bundled in static/vendor/three, no CDN.
 * SPDX-License-Identifier: AGPL-3.0-or-later
 */
import * as THREE from "three";
import {OrbitControls} from "./../vendor/three/OrbitControls.js";
import {TransformControls} from "./../vendor/three/TransformControls.js";
import {STLLoader} from "./../vendor/three/STLLoader.js";

const DEG = Math.PI / 180;

export function createScene(container) {
    const renderer = new THREE.WebGLRenderer({antialias: true, alpha: true});
    renderer.setPixelRatio(window.devicePixelRatio || 1);
    container.appendChild(renderer.domElement);
    const scene = new THREE.Scene();
    const camera = new THREE.PerspectiveCamera(45, 1, 1, 5000);
    camera.up.set(0, 0, 1);
    scene.add(new THREE.AmbientLight(0xffffff, 0.8));
    const sun = new THREE.DirectionalLight(0xffffff, 1.4);
    sun.position.set(200, -300, 500);
    scene.add(sun);

    const orbit = new OrbitControls(camera, renderer.domElement);
    orbit.enableDamping = false;
    const gizmo = new TransformControls(camera, renderer.domElement);
    gizmo.addEventListener("dragging-changed", (e) => { orbit.enabled = !e.value; });
    scene.add(gizmo.getHelper());

    const api = {state: {x: 0, y: 0, rx: 0, ry: 0, rz: 0, sx: 100, sy: 100, sz: 100},
                 volume: {width: 200, depth: 200, height: 200, center: false},
                 listeners: [], fits: true, size: null, pick: false};
    let mesh = null, bedGroup = new THREE.Group();
    scene.add(bedGroup);

    const render = () => renderer.render(scene, camera);
    const notify = () => api.listeners.forEach((cb) => cb(api));
    orbit.addEventListener("change", render);
    gizmo.addEventListener("change", render);

    api.resize = () => {
        const w = container.clientWidth || 600, h = container.clientHeight || 500;
        renderer.setSize(w, h);
        camera.aspect = w / h;
        camera.updateProjectionMatrix();
        render();
    };

    api.setVolume = (width, depth, height, center) => {
        const n = (v, d) => { v = parseFloat(String(v).replace(",", ".")); return v > 0 ? v : d; };
        const v = api.volume = {width: n(width, 200), depth: n(depth, 200), height: n(height, 200), center: !!center};
        bedGroup.clear();
        const cx = v.center ? 0 : v.width / 2, cy = v.center ? 0 : v.depth / 2;
        const plate = new THREE.Mesh(new THREE.PlaneGeometry(v.width, v.depth),
                                     new THREE.MeshBasicMaterial({color: 0x9aa4ae, transparent: true, opacity: 0.35, side: THREE.DoubleSide}));
        plate.position.set(cx, cy, -0.05);
        bedGroup.add(plate);
        const grid = new THREE.GridHelper(Math.max(v.width, v.depth), Math.round(Math.max(v.width, v.depth) / 10), 0x666666, 0x888888);
        grid.rotation.x = Math.PI / 2;
        grid.position.set(cx, cy, 0);
        bedGroup.add(grid);
        const box = new THREE.LineSegments(new THREE.EdgesGeometry(new THREE.BoxGeometry(v.width, v.depth, v.height)),
                                           new THREE.LineBasicMaterial({color: 0xe08a00}));
        box.position.set(cx, cy, v.height / 2);
        bedGroup.add(bedGroup.axes = new THREE.AxesHelper(Math.min(v.width, v.depth) / 4));
        bedGroup.axes.position.set(v.center ? -v.width / 2 : 0, v.center ? -v.depth / 2 : 0, 0);
        bedGroup.add(box);
        const dist = Math.max(v.width, v.depth, v.height) * 1.7;
        orbit.target.set(cx, cy, v.height / 4);
        camera.position.set(cx + dist * 0.5, cy - dist * 0.9, v.height / 2 + dist * 0.55);
        orbit.update();
        api.apply();
    };

    const bounds = () => new THREE.Box3().setFromObject(mesh);

    // applies the state (X/Y position, rotation in degrees, scale in %) while keeping the model resting on the build plate
    api.apply = () => {
        if (!mesh) { render(); return; }
        const s = api.state;
        mesh.rotation.set(s.rx * DEG, s.ry * DEG, s.rz * DEG, "XYZ");
        mesh.scale.set(s.sx / 100, s.sy / 100, s.sz / 100);
        mesh.position.set(s.x, s.y, 0);
        mesh.updateMatrixWorld(true);
        mesh.position.z -= bounds().min.z;
        mesh.updateMatrixWorld(true);
        const box = bounds(), v = api.volume;
        const lo = v.center ? [-v.width / 2, -v.depth / 2] : [0, 0];
        const eps = 0.01;
        api.size = box.getSize(new THREE.Vector3()).toArray();
        api.fits = box.min.x >= lo[0] - eps && box.max.x <= lo[0] + v.width + eps &&
                   box.min.y >= lo[1] - eps && box.max.y <= lo[1] + v.depth + eps && box.max.z <= v.height + eps;
        mesh.material.color.set(api.fits ? 0x4f8fd8 : 0xd8584f);
        render();
        notify();
    };

    const readBack = () => {
        const s = api.state;
        s.x = mesh.position.x; s.y = mesh.position.y;
        const e = new THREE.Euler().setFromQuaternion(mesh.quaternion, "XYZ");
        s.rx = e.x / DEG; s.ry = e.y / DEG; s.rz = e.z / DEG;
        s.sx = mesh.scale.x * 100; s.sy = mesh.scale.y * 100; s.sz = mesh.scale.z * 100;
        api.apply();
    };
    gizmo.addEventListener("objectChange", () => { if (mesh) readBack(); });

    api.setState = (patch) => { Object.assign(api.state, patch); api.apply(); };

    api.setMode = (mode) => {
        gizmo.setMode(mode);
        gizmo.showZ = mode !== "translate";
        render();
    };

    api.loadBuffer = (buffer) => {
        const geometry = new STLLoader().parse(buffer);
        geometry.computeBoundingBox();
        const c = geometry.boundingBox.getCenter(new THREE.Vector3());
        geometry.translate(-c.x, -c.y, -c.z);
        geometry.computeVertexNormals();
        if (mesh) { gizmo.detach(); scene.remove(mesh); mesh.geometry.dispose(); }
        mesh = new THREE.Mesh(geometry, new THREE.MeshStandardMaterial({color: 0x4f8fd8, metalness: 0.1, roughness: 0.6}));
        mesh.userData.pivot = c.toArray();
        scene.add(mesh);
        gizmo.attach(mesh);
        api.reset();
    };

    api.hasMesh = () => !!mesh;

    api.reset = () => {
        const v = api.volume;
        api.state = {x: v.center ? 0 : v.width / 2, y: v.center ? 0 : v.depth / 2, rx: 0, ry: 0, rz: 0, sx: 100, sy: 100, sz: 100};
        api.apply();
    };

    api.centerOnBed = () => {
        const v = api.volume;
        api.state.x = v.center ? 0 : v.width / 2;
        api.state.y = v.center ? 0 : v.depth / 2;
        api.apply();
    };

    // orients the local normal "n" downwards (the face rests on the build plate)
    const layNormalDown = (n) => {
        const q = new THREE.Quaternion().setFromUnitVectors(n.clone().normalize(), new THREE.Vector3(0, 0, -1));
        const e = new THREE.Euler().setFromQuaternion(q, "XYZ");
        Object.assign(api.state, {rx: e.x / DEG, ry: e.y / DEG, rz: e.z / DEG});
        api.apply();
    };

    api.layFlatLargest = () => {
        if (!mesh) return;
        const pos = mesh.geometry.attributes.position, groups = new Map();
        const a = new THREE.Vector3(), b = new THREE.Vector3(), c = new THREE.Vector3(), n = new THREE.Vector3();
        for (let i = 0; i + 2 < pos.count; i += 3) {
            a.fromBufferAttribute(pos, i); b.fromBufferAttribute(pos, i + 1); c.fromBufferAttribute(pos, i + 2);
            n.crossVectors(b.clone().sub(a), c.clone().sub(a));
            const area = n.length() / 2;
            if (area < 1e-9) continue;
            n.normalize();
            const key = n.toArray().map((x) => Math.round(x * 100)).join(",");
            const g = groups.get(key) || {area: 0, n: n.clone()};
            g.area += area;
            groups.set(key, g);
        }
        let best = null;
        groups.forEach((g) => { if (!best || g.area > best.area) best = g; });
        if (best) layNormalDown(best.n);
    };

    // "lay flat" on the clicked face
    api.setPickMode = (on) => { api.pick = !!on; renderer.domElement.style.cursor = on ? "crosshair" : ""; };
    let down = null;
    renderer.domElement.addEventListener("pointerdown", (e) => { down = [e.clientX, e.clientY]; });
    renderer.domElement.addEventListener("pointerup", (e) => {
        if (!api.pick || !mesh || !down || Math.hypot(e.clientX - down[0], e.clientY - down[1]) > 4) return;
        const r = renderer.domElement.getBoundingClientRect();
        const ndc = new THREE.Vector2(((e.clientX - r.left) / r.width) * 2 - 1, -((e.clientY - r.top) / r.height) * 2 + 1);
        const ray = new THREE.Raycaster();
        ray.setFromCamera(ndc, camera);
        const hit = ray.intersectObject(mesh)[0];
        if (hit && hit.face) { layNormalDown(hit.face.normal); api.setPickMode(false); notify(); }
    });

    // --- Layer preview of a G-code (colors by type, like Cura)
    const COLORS = {"WALL-OUTER": 0xff3030, "WALL-INNER": 0x30c030, "SKIN": 0xffe000, "FILL": 0xff9020,
                    "SUPPORT": 0x00d8e8, "SUPPORT-INTERFACE": 0x3060ff, "SKIRT": 0x40a0ff, "PRIME-TOWER": 0xc060ff, "TRAVEL": 0x8a8a8a};
    const LABELS = {"WALL-OUTER": "Outer wall", "WALL-INNER": "Inner wall", "SKIN": "Skin",
                    "FILL": "Infill", "SUPPORT": "Support", "SUPPORT-INTERFACE": "Support interface",
                    "SKIRT": "Skirt / brim", "PRIME-TOWER": "Prime tower", "TRAVEL": "Travel"};
    const preview = new THREE.Group();
    preview.visible = false;
    scene.add(preview);
    api.layerCount = 0;

    api.clearGcode = () => {
        preview.children.forEach((l) => l.geometry.dispose());
        preview.clear();
        api.layerCount = 0;
        api.layerTypes = [];
    };

    // Parses the G-code: extrusion segments by type and by layer
    api.loadGcode = (text) => {
        api.clearGcode();
        const types = {};            // type -> {pos: [], ends: []}
        let layer = -1, type = "OTHER", x = 0, y = 0, z = 0, e = 0, relative = false, count = 0;
        const lines = text.split("\n");
        const endLayer = () => { Object.values(types).forEach((t) => { while (t.ends.length <= layer) t.ends.push(t.pos.length / 3); }); };
        for (let i = 0; i < lines.length; i++) {
            const line = lines[i];
            const c = line.charCodeAt(0);
            if (c === 59) {                                   // ";"
                if (line.startsWith(";LAYER:")) {
                    const n = parseInt(line.substr(7), 10);
                    if (n >= 0) { if (layer >= 0) endLayer(); layer = n; count = Math.max(count, n + 1); }
                } else if (line.startsWith(";TYPE:")) type = line.substr(6).trim();
                continue;
            }
            if (c !== 71 && c !== 77) continue;               // G / M
            if (line.startsWith("M82")) { relative = false; continue; }
            if (line.startsWith("M83")) { relative = true; continue; }
            if (line.startsWith("G92")) { const m = /E(-?[\d.]+)/.exec(line); if (m) e = parseFloat(m[1]); continue; }
            if (!(line.startsWith("G1 ") || line.startsWith("G0 "))) continue;
            let nx = x, ny = y, nz = z, ne = null, m;
            if ((m = /X(-?[\d.]+)/.exec(line))) nx = parseFloat(m[1]);
            if ((m = /Y(-?[\d.]+)/.exec(line))) ny = parseFloat(m[1]);
            if ((m = /Z(-?[\d.]+)/.exec(line))) nz = parseFloat(m[1]);
            if ((m = /E(-?[\d.]+)/.exec(line))) ne = parseFloat(m[1]);
            let extrudes = false;
            if (ne !== null) { extrudes = relative ? ne > 0 : ne > e; e = relative ? e : ne; }
            if (layer >= 0 && (nx !== x || ny !== y)) {
                const key = extrudes ? type : "TRAVEL";
                const t = types[key] || (types[key] = {pos: [], ends: []});
                t.pos.push(x, y, z, nx, ny, nz);
            }
            x = nx; y = ny; z = nz;
        }
        if (layer >= 0) endLayer();
        api.layerTypes = [];
        Object.keys(types).forEach((name) => {
            const t = types[name];
            while (t.ends.length < count) t.ends.push(t.pos.length / 3);
            const geometry = new THREE.BufferGeometry();
            geometry.setAttribute("position", new THREE.Float32BufferAttribute(t.pos, 3));
            const line = new THREE.LineSegments(geometry, new THREE.LineBasicMaterial({color: COLORS[name] || 0xaaaaaa}));
            line.userData = {type: name, ends: t.ends};
            // the current layer is redrawn lighter (same geometry, different vertex range)
            const shared = new THREE.BufferGeometry();
            shared.setAttribute("position", geometry.getAttribute("position"));   // shared attribute, own range
            const bright = new THREE.LineSegments(shared, new THREE.LineBasicMaterial({
                color: new THREE.Color(COLORS[name] || 0xaaaaaa).lerp(new THREE.Color(0xffffff), 0.65)}));
            bright.userData = {type: name, highlight: true, ends: t.ends};
            if (name === "TRAVEL") { line.visible = false; bright.visible = false; }
            preview.add(line);
            preview.add(bright);
            api.layerTypes.push({type: name, label: LABELS[name] || name, color: "#" + (COLORS[name] || 0xaaaaaa).toString(16).padStart(6, "0")});
        });
        api.layerCount = count;
        api.setLayer(count);
        return count;
    };

    // shows layers 1..n (n = number of visible layers)
    api.setLayer = (n) => {
        n = Math.max(1, Math.min(api.layerCount, n));
        preview.children.forEach((l) => {
            const ends = l.userData.ends, end = ends[n - 1] !== undefined ? ends[n - 1] : 0;
            if (l.userData.highlight) {
                const start = n > 1 && ends[n - 2] !== undefined ? ends[n - 2] : 0;
                l.geometry.setDrawRange(start, end - start);
            } else {
                l.geometry.setDrawRange(0, end);
            }
        });
        render();
    };
    api.setTypeVisible = (type, on) => {
        preview.children.forEach((l) => { if (l.userData.type === type) l.visible = !!on; });
        render();
    };
    api.setPreview = (on) => {
        on = !!on && api.layerCount > 0;
        preview.visible = on;
        if (mesh) mesh.visible = !on;
        gizmo.enabled = !on;
        gizmo.getHelper().visible = !on;
        render();
    };

    api.setMode("translate");
    api.setVolume(200, 200, 200, false);
    window.addEventListener("resize", api.resize);
    if (window.ResizeObserver) new ResizeObserver(() => api.resize()).observe(container);
    api.resize();
    return api;
}
