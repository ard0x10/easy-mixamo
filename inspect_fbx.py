"""
Pre-flight check: look at a folder of FBX files before building.

  blender --factory-startup -b --python inspect_fbx.py -- --src <folder>

The important part of the output is the REST POSE COMPARISON table. If the skinned file
and the animation files have different rest poses (very common with Mixamo), copying
actions across would twist the arms and hands. build_character.py already handles that
correctly; this script exists so you can see what you are dealing with beforehand.
"""

import bpy, os, sys, argparse

MIN_BLENDER = (4, 2, 0)


def parse_args():
    argv = sys.argv
    argv = argv[argv.index("--") + 1:] if "--" in argv else []
    p = argparse.ArgumentParser(prog="inspect_fbx.py")
    p.add_argument("--src", required=True, help="folder containing the FBX files")
    p.add_argument("--bones", type=int, default=8, help="how many sample bones to list")
    return p.parse_args(argv)


def log(*a):
    print(*a)
    sys.stdout.flush()


def strip(n):
    return n.split(":")[-1]


def read(path):
    bpy.ops.wm.read_factory_settings(use_empty=True)
    bpy.ops.import_scene.fbx(filepath=path)
    scn = bpy.context.scene
    arms = [o for o in scn.objects if o.type == 'ARMATURE']
    meshes = [o for o in scn.objects if o.type == 'MESH']
    info = dict(arms=len(arms), meshes=len(meshes), fps=scn.render.fps,
                verts=sum(len(m.data.vertices) for m in meshes),
                materials=[m.name for m in bpy.data.materials],
                images=[(i.name, tuple(i.size)) for i in bpy.data.images
                        if i.name != 'Render Result'],
                actions=[(a.name, int(a.frame_range[0]), int(a.frame_range[1]))
                         for a in bpy.data.actions],
                rest=None, prop=None, scale=None, roots=None, prefixes=None, connected=0)
    if arms:
        a = arms[0]
        info["scale"] = tuple(round(v, 6) for v in a.matrix_world.to_scale())
        info["roots"] = [strip(b.name) for b in a.data.bones if b.parent is None]
        info["prefixes"] = sorted({b.name.rsplit(":", 1)[0] + ":"
                                   for b in a.data.bones if ":" in b.name})
        info["connected"] = sum(1 for b in a.data.bones if b.use_connect)
        mw = a.matrix_world
        info["rest"] = {strip(b.name): (mw @ b.matrix_local).translation.copy()
                        for b in a.data.bones}
        # Distance from each joint to its parent joint - the rig's PROPORTIONS, and the
        # measure build_character.py uses. bone.length would be wrong here: Mixamo's leaf
        # bones ('LeftHandIndex4', 'HeadTop_End') are tip markers re-derived on every
        # download, so they differ by tens of percent between two files of the same
        # character while nothing that deforms has moved. Leaves are skipped.
        head = {b.name: (mw @ b.matrix_local).translation for b in a.data.bones}
        info["prop"] = {strip(b.name): (head[b.name] - head[b.parent.name]).length
                        for b in a.data.bones if b.parent is not None and b.children}
        info["bones"] = [strip(b.name) for b in a.data.bones]
    return info


def main():
    if bpy.app.version < MIN_BLENDER:
        log(f"ERROR: Blender {'.'.join(map(str, MIN_BLENDER))} or newer is required, "
            f"this is {'.'.join(map(str, bpy.app.version))}")
        sys.exit(1)

    args = parse_args()
    src = os.path.abspath(args.src)
    files = sorted(f for f in os.listdir(src) if f.lower().endswith(".fbx"))
    if not files:
        log(f"no .fbx files in {src}")
        return

    data = {}
    for f in files:
        data[f] = read(os.path.join(src, f))

    log("\n" + "=" * 78)
    log("FILES")
    log("=" * 78)
    for f, d in data.items():
        kind = "SKINNED (has a mesh)" if d["meshes"] else "animation-only (no mesh)"
        log(f"\n{f}   -> {kind}")
        log(f"  armatures={d['arms']}  meshes={d['meshes']}  verts={d['verts']}  fps={d['fps']}")
        if d["arms"]:
            log(f"  bones={len(d['bones'])}  roots={d['roots']}  connected={d['connected']}")
            log(f"  object scale={d['scale']}   name prefix={d['prefixes'] or '(none)'}")
            log(f"  sample bones: {d['bones'][:args.bones]}")
        if d["actions"]:
            for n, a, b in d["actions"]:
                log(f"  action '{n}'  frames {a}..{b}  ({b - a + 1} frames, "
                    f"{(b - a + 1) / max(d['fps'], 1):.2f} s)")
        else:
            log("  NO action")
        if d["materials"]:
            log(f"  materials={d['materials']}  textures={d['images']}")

    skins = [f for f, d in data.items() if d["meshes"]]
    rigged = [f for f, d in data.items() if d["arms"]]
    log("\n" + "=" * 78)
    log("COMPATIBILITY")
    log("=" * 78)
    if len(skins) != 1:
        log(f"!! {len(skins)} files contain a mesh {skins} - build_character.py expects "
            f"exactly one skinned file")
        log("   use --skin to say which one to use")
    else:
        log(f"skinned file: {skins[0]}")
    if len(rigged) < 2:
        log("no second rig to compare against")
        return

    base = skins[0] if skins else rigged[0]
    bb = set(data[base]["bones"])

    log(f"\nbone SET (reference: '{base}'):")
    for f in rigged:
        s = set(data[f]["bones"])
        miss, extra = bb - s, s - bb
        verdict = "EXACT MATCH" if not miss and not extra else "DIFFERENT"
        log(f"  {f:24s} {verdict}")
        if miss:
            log(f"      missing : {sorted(miss)}")
        if extra:
            log(f"      extra   : {sorted(extra)}  (ignored during the build)")

    log(f"\nbone PROPORTIONS (reference: '{base}')  -> is it the same skeleton?")
    log("  (joint-to-parent-joint distances; leaf 'tip' bones are ignored, Mixamo "
        "re-derives them)")
    for f in rigged:
        if f == base:
            continue
        common = set(data[base]["prop"]) & set(data[f]["prop"])
        worst, wb = 0.0, None
        for n in common:
            a_, b_ = data[base]["prop"][n], data[f]["prop"][n]
            r = abs(a_ - b_) / max(a_, b_, 1e-9)
            if r > worst:
                worst, wb = r, n
        ok = "SAME SKELETON" if worst <= 0.01 else "DIFFERENT SKELETON -> needs real retargeting"
        log(f"  {f:24s} max diff {worst*100:.3f}% ({wb})  {ok}")

    log(f"\nREST POSE (reference: '{base}')  -> can actions be moved across directly?")
    log("  (if the gap is large, NEVER copy channels; build_character.py solves in world space)")
    for f in rigged:
        if f == base:
            continue
        common = bb & set(data[f]["bones"])
        worst, wb = 0.0, None
        for n in common:
            d = (data[base]["rest"][n] - data[f]["rest"][n]).length
            if d > worst:
                worst, wb = d, n
        if worst < 1e-4:
            verdict = "SAME - copying channels would have been safe too"
        else:
            verdict = "DIFFERENT -> copying channels would twist the arms and hands"
        log(f"  {f:24s} max diff {worst:8.4f} m ({wb})  {verdict}")

    fps = {d["fps"] for d in data.values()}
    if len(fps) > 1:
        log(f"\n!! fps values differ: {fps} - pin it with --fps during the build")

    log("\n" + "=" * 78)
    log("SUGGESTED COMMAND")
    log("=" * 78)
    anims = " ".join(f'--anim "{f}={os.path.splitext(f)[0]}"'
                     for f in files if f != base)
    log(f'  --src "{src}" --skin "{base}" {anims}')
    log("")


if __name__ == "__main__":
    main()
