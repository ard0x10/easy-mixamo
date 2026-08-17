"""
Render preview stills from a GLB, so you can check the result with your own eyes.

  blender --factory-startup -b --python render_preview.py -- --glb <file.glb> --out <folder>

Produces the rest pose, a few frames from every animation, and a close-up of a hand.
The rest pose shot and the hand close-up matter most: a rest pose mismatch shows up
there before anywhere else.
"""

import bpy, os, sys, math, argparse
from mathutils import Vector

MIN_BLENDER = (4, 2, 0)


def parse_args():
    argv = sys.argv
    argv = argv[argv.index("--") + 1:] if "--" in argv else []
    p = argparse.ArgumentParser(prog="render_preview.py")
    p.add_argument("--glb", required=True)
    p.add_argument("--out", default=None, help="output folder (default: <glb>_preview)")
    p.add_argument("--shots", type=int, default=3, help="frames rendered per animation")
    p.add_argument("--res", type=int, default=460)
    p.add_argument("--hand-bone", default=None,
                   help="bone for the close-up (default: the first bone with 'hand' in its name)")
    return p.parse_args(argv)


def main():
    if bpy.app.version < MIN_BLENDER:
        print(f"ERROR: Blender {'.'.join(map(str, MIN_BLENDER))} or newer is required, "
              f"this is {'.'.join(map(str, bpy.app.version))}")
        sys.exit(1)

    args = parse_args()
    glb = os.path.abspath(args.glb)
    out = os.path.abspath(args.out) if args.out else os.path.splitext(glb)[0] + "_preview"
    os.makedirs(out, exist_ok=True)

    bpy.ops.wm.read_factory_settings(use_empty=True)
    bpy.ops.import_scene.gltf(filepath=glb)
    scn = bpy.context.scene
    arm = next(o for o in scn.objects if o.type == 'ARMATURE')
    # Drop the glTF importer's 'glTF_not_exported' helper objects, otherwise the
    # framing is computed around them.
    meshes = [o for o in scn.objects if o.type == 'MESH'
              and not any(c.name == 'glTF_not_exported' for c in o.users_collection)]
    for o in scn.objects:
        if o.type == 'MESH' and o not in meshes:
            bpy.data.objects.remove(o, do_unlink=True)

    world = bpy.data.worlds.new("W")
    scn.world = world
    # World.use_nodes is going away in Blender 6.0; only touch it if we have to
    if getattr(world, "node_tree", None) is None:
        world.use_nodes = True
    world.node_tree.nodes["Background"].inputs[0].default_value = (0.06, 0.07, 0.10, 1)

    for energy, rot in ((4.5, (50, 0, 30)), (1.5, (60, 0, 200))):
        L = bpy.data.objects.new("L", bpy.data.lights.new("L", 'SUN'))
        L.data.energy = energy
        L.rotation_euler = tuple(math.radians(v) for v in rot)
        scn.collection.objects.link(L)

    cam = bpy.data.objects.new("Cam", bpy.data.cameras.new("Cam"))
    scn.collection.objects.link(cam)
    scn.camera = cam
    scn.render.image_settings.file_format = 'PNG'
    try:
        scn.render.engine = 'BLENDER_EEVEE_NEXT'
    except TypeError:
        scn.render.engine = 'BLENDER_EEVEE'

    def bounds():
        bpy.context.view_layer.update()
        dg = bpy.context.evaluated_depsgraph_get()
        co = []
        for m in meshes:
            ev = m.evaluated_get(dg)
            co += [ev.matrix_world @ v.co for v in ev.data.vertices]
        lo = Vector((min(c.x for c in co), min(c.y for c in co), min(c.z for c in co)))
        hi = Vector((max(c.x for c in co), max(c.y for c in co), max(c.z for c in co)))
        return lo, hi

    def rest_pose():
        if arm.animation_data:
            arm.animation_data.action = None
        for pb in arm.pose.bones:
            pb.rotation_mode = 'QUATERNION'
            pb.location = (0, 0, 0)
            pb.rotation_quaternion = (1, 0, 0, 0)
            pb.scale = (1, 1, 1)
        bpy.context.view_layer.update()

    def set_anim(act, frame):
        ad = arm.animation_data or arm.animation_data_create()
        ad.action = act
        if hasattr(ad, "action_slot") and ad.action_slot is None and act.slots:
            ad.action_slot = act.slots[0]
        scn.frame_set(frame)
        bpy.context.view_layer.update()

    def aim(center, dist, height_frac, lens=50):
        cam.data.lens = lens
        cam.location = center + Vector((dist * 0.45, -dist, dist * height_frac))
        cam.rotation_euler = (center - cam.location).to_track_quat('-Z', 'Y').to_euler()

    def safe(name):
        # action names straight out of an FBX look like 'Armature|mixamo.com|Base Layer';
        # '|' and ':' are not allowed in filenames on Windows
        return "".join("_" if c in '\\/:*?"<>|' else c for c in name).strip(" .") or "action"

    def shoot(name, w, h):
        scn.render.resolution_x, scn.render.resolution_y = w, h
        scn.render.filepath = os.path.join(out, name)
        bpy.ops.render.render(write_still=True)
        print("[render]", name)
        sys.stdout.flush()

    rest_pose()
    lo, hi = bounds()
    size = max(hi.x - lo.x, hi.z - lo.z)
    dist = size * 2.4
    center = Vector(((lo.x + hi.x) / 2, (lo.y + hi.y) / 2, (lo.z + hi.z) / 2))

    # rest pose, straight on
    cam.data.lens = 50
    cam.location = center + Vector((0, -dist, 0))
    cam.rotation_euler = (center - cam.location).to_track_quat('-Z', 'Y').to_euler()
    shoot("00_rest_front.png", args.res, int(args.res * 1.3))

    for act in sorted(bpy.data.actions, key=lambda a: a.name):
        a, b = int(act.frame_range[0]), int(act.frame_range[1])
        n = max(1, min(args.shots, b - a + 1))
        frames = [a + round(i * (b - a) / max(n - 1, 1)) for i in range(n)] if n > 1 else [a]
        for f in frames:
            set_anim(act, f)
            lo2, hi2 = bounds()
            c = Vector(((lo2.x + hi2.x) / 2, (lo2.y + hi2.y) / 2, center.z))
            aim(c, dist, 0.12)
            shoot(f"{safe(act.name)}_{f:04d}.png", args.res, int(args.res * 1.3))

    # hand close-up - a rest pose mismatch shows up here first
    hand = args.hand_bone
    if hand is None:
        hand = next((b.name for b in arm.data.bones if "hand" in b.name.lower()
                     and not any(ch.isdigit() for ch in b.name)), None)
    if hand and hand in arm.pose.bones:
        # middle frame of the longest animation (Action.fcurves does not always exist
        # in Blender 5.x, so pick by duration instead)
        longest = max(bpy.data.actions, key=lambda a: a.frame_range[1] - a.frame_range[0])
        mid = int((longest.frame_range[0] + longest.frame_range[1]) / 2)
        for tag, setup in (("rest", rest_pose),
                           (f"anim_{safe(longest.name)}", lambda: set_anim(longest, mid))):
            setup()
            p = (arm.matrix_world @ arm.pose.bones[hand].matrix).translation
            d = size * 0.28
            cam.data.lens = 70
            cam.location = p + Vector((d * 0.3, -d, d * 0.25))
            cam.rotation_euler = (p - cam.location).to_track_quat('-Z', 'Y').to_euler()
            shoot(f"90_hand_{tag}.png", args.res, args.res)
    else:
        print("[render] no hand bone found, close-up skipped (use --hand-bone to name one)")

    print(f"[render] done -> {out}")


if __name__ == "__main__":
    main()
