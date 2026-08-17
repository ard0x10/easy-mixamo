"""
Independently verify a generated GLB.

  blender --factory-startup -b --python verify_glb.py -- --glb <file.glb>

Two stages: first read the raw glTF JSON (what is actually in the file, not what Blender
thinks it wrote), then load it back into a clean scene and play the animations. Things
that specifically cause headaches in Godot are flagged separately.
Exit code: 1 if any problem was found.
"""

import bpy, os, sys, json, struct, argparse
from mathutils import Vector

MIN_BLENDER = (4, 2, 0)
PROBLEMS = []


def parse_args():
    argv = sys.argv
    argv = argv[argv.index("--") + 1:] if "--" in argv else []
    p = argparse.ArgumentParser(prog="verify_glb.py")
    p.add_argument("--glb", required=True)
    p.add_argument("--expect-anims", default=None,
                   help="comma separated list of animation names that must be present")
    p.add_argument("--expect-height", type=float, default=None,
                   help="expected height in metres, 2%% tolerance")
    return p.parse_args(argv)


def log(*a):
    print(*a)
    sys.stdout.flush()


def problem(msg):
    PROBLEMS.append(msg)
    log(f"  !! {msg}")


def read_chunks(path):
    with open(path, "rb") as f:
        data = f.read()
    magic, ver, total = struct.unpack("<III", data[:12])
    if magic != 0x46546C67:
        raise SystemExit("this is not a GLB file")
    off, js, bin_chunk = 12, None, None
    while off < total:
        clen, ctype = struct.unpack("<II", data[off:off + 8])
        payload = data[off + 8:off + 8 + clen]
        if ctype == 0x4E4F534A:
            js = json.loads(payload.decode("utf-8"))
        elif ctype == 0x004E4942:
            bin_chunk = payload
        off += 8 + clen
    return js, bin_chunk


def main():
    if bpy.app.version < MIN_BLENDER:
        log(f"ERROR: Blender {'.'.join(map(str, MIN_BLENDER))} or newer is required, "
            f"this is {'.'.join(map(str, bpy.app.version))}")
        sys.exit(1)

    args = parse_args()
    glb = os.path.abspath(args.glb)
    js, binc = read_chunks(glb)

    log("=" * 74)
    log(f"RAW glTF   {os.path.basename(glb)}   ({os.path.getsize(glb)} bytes)")
    log("=" * 74)
    log(f"  generator : {js['asset'].get('generator')}")
    log(f"  nodes={len(js.get('nodes', []))}  meshes={len(js.get('meshes', []))}  "
        f"skins={len(js.get('skins', []))}  materials={len(js.get('materials', []))}  "
        f"images={len(js.get('images', []))}  animations={len(js.get('animations', []))}")

    anim_names = [a.get("name") for a in js.get("animations", [])]
    for a in js.get("animations", []):
        log(f"  animation '{a.get('name')}'  {len(a['channels'])} channels")
    if not anim_names:
        problem("the GLB contains no animations")

    for s in js.get("skins", []):
        log(f"  skin: {len(s['joints'])} joints")
    if not js.get("skins"):
        problem("no skin - the mesh is not bound to the skeleton")

    bad = [n.get("name") for n in js.get("nodes", []) if n.get("name") and ":" in n["name"]]
    if bad:
        problem(f"node names contain ':' (the Godot NodePath separator): {bad[:5]}")

    for m in js.get("materials", []):
        mode = m.get("alphaMode", "OPAQUE")
        log(f"  material '{m.get('name')}'  alphaMode={mode}  doubleSided={m.get('doubleSided')}")
        if mode == "BLEND":
            problem(f"material '{m.get('name')}' has alphaMode=BLEND - if its texture is "
                    f"fully opaque, Godot puts it in the transparent pass for nothing")

    for i in js.get("images", []):
        log(f"  image '{i.get('name')}'  {i.get('mimeType')}  "
            f"{'embedded' if 'bufferView' in i else 'EXTERNAL FILE'}")
        if "bufferView" not in i:
            problem(f"image '{i.get('name')}' is not embedded in the GLB - not a single file")

    # ---------------------------------------------------------------- load it back
    log("")
    log("=" * 74)
    log("RELOADING INTO A CLEAN SCENE")
    log("=" * 74)
    bpy.ops.wm.read_factory_settings(use_empty=True)
    bpy.ops.import_scene.gltf(filepath=glb)
    scn = bpy.context.scene
    arms = [o for o in scn.objects if o.type == 'ARMATURE']
    # Blender's glTF IMPORTER creates helper objects and puts them in a
    # 'glTF_not_exported' collection (e.g. a 42-vert Icosphere). They are not in the
    # file - do not count them.
    meshes = [o for o in scn.objects if o.type == 'MESH'
              and not any(c.name == 'glTF_not_exported' for c in o.users_collection)]
    helpers = [o.name for o in scn.objects if o.type == 'MESH' and o not in meshes]
    if helpers:
        log(f"  (ignored import artefacts: {helpers})")
    if len(meshes) != len(js.get("meshes", [])):
        problem(f"mesh count in the scene ({len(meshes)}) does not match the glTF "
                f"({len(js.get('meshes', []))})")
    if len(arms) != 1:
        problem(f"found {len(arms)} armatures, expected 1")
        return finish()
    arm, = arms
    log(f"  armature '{arm.name}'  bones={len(arm.data.bones)}  meshes={len(meshes)}")
    log(f"  root bones: {[b.name for b in arm.data.bones if b.parent is None]}")

    s = arm.matrix_world.to_scale()
    log(f"  armature object scale: {tuple(round(v, 6) for v in s)}")
    if max(abs(v - 1.0) for v in s) > 1e-4:
        problem(f"armature object scale is not 1.0 {tuple(s)} - Skeleton3D arrives scaled "
                f"in Godot, which breaks attaching items with BoneAttachment3D")

    for m in meshes:
        if not any(x.type == 'ARMATURE' for x in m.modifiers):
            problem(f"mesh '{m.name}' has no armature modifier")

    # height in the rest pose
    for pb in arm.pose.bones:
        pb.rotation_mode = 'QUATERNION'
        pb.location = (0, 0, 0)
        pb.rotation_quaternion = (1, 0, 0, 0)
        pb.scale = (1, 1, 1)
    bpy.context.view_layer.update()
    dg = bpy.context.evaluated_depsgraph_get()
    co = []
    for m in meshes:
        ev = m.evaluated_get(dg)
        co += [ev.matrix_world @ v.co for v in ev.data.vertices]
    lo = Vector((min(c.x for c in co), min(c.y for c in co), min(c.z for c in co)))
    hi = Vector((max(c.x for c in co), max(c.y for c in co), max(c.z for c in co)))
    h = hi.z - lo.z
    log(f"  height in rest pose {h:.3f} m   feet z={lo.z:+.4f}")
    if args.expect_height and abs(h - args.expect_height) / args.expect_height > 0.02:
        problem(f"height is {h:.3f} m, expected {args.expect_height:.3f} m")
    if abs(lo.z) > 0.05 * h:
        log(f"  note: the feet are not at z=0 ({lo.z:+.3f} m) - you may need to offset "
            f"the model in Godot")

    root = next(b.name for b in arm.data.bones if b.parent is None)
    log("")
    for act in sorted(bpy.data.actions, key=lambda a: a.name):
        ad = arm.animation_data or arm.animation_data_create()
        ad.action = act
        if hasattr(ad, "action_slot") and ad.action_slot is None and act.slots:
            ad.action_slot = act.slots[0]
        a, b = int(act.frame_range[0]), int(act.frame_range[1])

        # Sample EVERY bone, not just the root: an in-place animation keeps the root
        # still on purpose, so a root-only test would call it empty. Five samples,
        # because a cyclic motion can happen to return to its start at the midpoint.
        samples = sorted({int(round(a + i * (b - a) / 4)) for i in range(5)})
        poses = []
        for f in samples:
            scn.frame_set(f)
            bpy.context.view_layer.update()
            poses.append({pb.name: (arm.matrix_world @ pb.matrix).translation.copy()
                          for pb in arm.pose.bones})

        first = poses[0]
        rm = max(Vector(((p[root] - first[root]).x, (p[root] - first[root]).y)).length
                 for p in poses)
        moving = max((p[n] - first[n]).length for p in poses[1:] for n in p) \
            if len(poses) > 1 else 0.0
        secs = (b - a + 1) / max(scn.render.fps, 1)
        log(f"  '{act.name}'  frames {a}..{b} ({secs:.2f} s)  root motion {rm:6.3f} m"
            + ("  <-- not in place" if rm > 0.05 else ""))
        if moving < 1e-6 and (b - a) > 2:
            problem(f"animation '{act.name}' never moves any bone - it may be empty")

    if args.expect_anims:
        want = {x.strip() for x in args.expect_anims.split(",") if x.strip()}
        got = {a.name for a in bpy.data.actions}
        if want - got:
            problem(f"expected animations are missing: {sorted(want - got)} "
                    f"(found: {sorted(got)})")

    return finish()


def finish():
    log("")
    log("=" * 74)
    if PROBLEMS:
        log(f"{len(PROBLEMS)} PROBLEM(S)")
        for p in PROBLEMS:
            log(f"  - {p}")
        log("=" * 74)
        sys.exit(1)
    log("NO PROBLEMS")
    log("=" * 74)


if __name__ == "__main__":
    main()
