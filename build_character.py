"""
Mixamo FBX files -> one GLB (Godot-ready).

Usage:
  blender --factory-startup -b --python build_character.py -- --src <folder> [options]

Why this is more involved than it looks: the skinned FBX and the animation-only FBX
files do not necessarily share the same REST (bind) pose. Blender stores pose channels
RELATIVE to rest, so moving an action from one rig to another silently corrupts it.
This script never copies channels. It samples every source rig's bone matrices in a
common world space, frame by frame, then solves the target rig's pose to match those
matrices. See README.md for the full explanation.
"""

import bpy, os, sys, argparse
from mathutils import Matrix, Vector

MIN_BLENDER = (4, 2, 0)

# --------------------------------------------------------------------------- cli


def parse_args():
    argv = sys.argv
    argv = argv[argv.index("--") + 1:] if "--" in argv else []

    p = argparse.ArgumentParser(prog="build_character.py")
    p.add_argument("--src", required=True,
                   help="folder containing the FBX files")
    p.add_argument("--out", default=None,
                   help="output .glb path (default: <src>/Character.glb)")
    p.add_argument("--skin", default=None,
                   help="the skinned FBX, optionally with '=Name'. If omitted, the first "
                        "file containing a mesh is used")
    p.add_argument("--anim", action="append", default=[],
                   help="animation FBX, 'File.fbx' or 'File.fbx=Name'. Repeatable. "
                        "If omitted, every FBX except the skinned one is used")
    p.add_argument("--rest", default="auto",
                   help="new rest pose: 'auto' (the animation rigs' rest pose), "
                        "'bind' (leave it alone), or '<AnimName>:<frame>'")
    p.add_argument("--target-height", type=float, default=None,
                   help="scale the character to this height in metres, e.g. 1.75. "
                        "Default: keep the source scale")
    p.add_argument("--in-place", action="append", default=[],
                   help="strip horizontal root motion from this animation. Repeatable")
    p.add_argument("--root-bone", default=None,
                   help="bone used to measure root motion (default: the skeleton's root bone)")
    p.add_argument("--fps", type=int, default=None,
                   help="scene fps (default: the fps of the first FBX)")
    p.add_argument("--keep-prefix", action="store_true",
                   help="keep prefixes such as 'mixamorig:' in bone names "
                        "(not recommended for Godot)")
    p.add_argument("--blend", default=None,
                   help="save a .blend after the build (default: alongside <out>)")
    p.add_argument("--tolerance", type=float, default=1e-4,
                   help="verification threshold in metres (default 1e-4 = 0.1 mm)")
    return p.parse_args(argv)


def split_spec(spec):
    """'File.fbx=Name' -> ('File.fbx', 'Name');  'File.fbx' -> ('File.fbx', 'File')"""
    if "=" in spec:
        f, n = spec.split("=", 1)
        return f.strip(), n.strip()
    return spec.strip(), os.path.splitext(os.path.basename(spec.strip()))[0]


# --------------------------------------------------------------------------- util

def log(*a):
    print("[build]", *a)
    sys.stdout.flush()


class BuildError(RuntimeError):
    pass


def check_blender_version():
    if bpy.app.version < MIN_BLENDER:
        raise BuildError(
            f"Blender {'.'.join(map(str, MIN_BLENDER))} or newer is required, "
            f"this is {'.'.join(map(str, bpy.app.version))}")


def sanitize(name):
    """Strip characters that cause trouble inside a Godot NodePath."""
    n = name.split(":")[-1]                      # 'mixamorig:Hips' -> 'Hips'
    for ch in '/.[]"':
        n = n.replace(ch, "_")
    return n


def assign_action(ob, act):
    """Blender 4.4+ slotted actions: without a slot the action drives nothing."""
    ad = ob.animation_data or ob.animation_data_create()
    ad.action = act
    if hasattr(ad, "action_slot") and ad.action_slot is None and getattr(act, "slots", None):
        ad.action_slot = act.slots[0]


def clear_pose(arm):
    for pb in arm.pose.bones:
        pb.rotation_mode = 'QUATERNION'
        pb.location = (0, 0, 0)
        pb.rotation_quaternion = (1, 0, 0, 0)
        pb.scale = (1, 1, 1)
    bpy.context.view_layer.update()


def import_fbx(path):
    before = set(bpy.context.scene.objects)
    bpy.ops.import_scene.fbx(filepath=path)
    return [o for o in bpy.context.scene.objects if o not in before]


def capture(arm, act, frames, name_map=sanitize):
    """Sample bone matrices in a COMMON space: metres, Z-up, orthonormal 3x3.

    arm.matrix_world carries the FBX 0.01 unit scale in its 3x3 part. That scale is
    not cancelled out when the object matrix later becomes identity, so it would leak
    into the solver. Divide it out at capture time.
    """
    s = arm.matrix_world.to_scale()
    if max(abs(s.x - s.y), abs(s.x - s.z)) > 1e-6:
        raise BuildError(f"armature object scale is not uniform: {tuple(s)}")
    if act is not None:
        assign_action(arm, act)
    out = {}
    for f in frames:
        bpy.context.scene.frame_set(int(f))
        bpy.context.view_layer.update()
        row = {}
        for pb in arm.pose.bones:
            loc, rot, scl = (arm.matrix_world @ pb.matrix).decompose()
            row[name_map(pb.name)] = Matrix.LocRotScale(loc, rot, scl / s.x)
        out[int(f)] = row
    return out


def solve_basis(target, arm):
    """matrix_basis values that put every bone on its target matrix.
    arm.matrix_world must be identity (the targets already live in that space)."""
    out = {}
    for pb in arm.pose.bones:
        b = pb.bone
        if b.parent:
            rest_rel = b.parent.matrix_local.inverted() @ b.matrix_local
            out[b.name] = (target[b.parent.name] @ rest_rel).inverted() @ target[b.name]
        else:
            out[b.name] = b.matrix_local.inverted() @ target[b.name]
    return out


def apply_basis(arm, basis):
    for pb in arm.pose.bones:
        pb.rotation_mode = 'QUATERNION'
        loc, rot, scl = basis[pb.bone.name].decompose()
        pb.location, pb.rotation_quaternion, pb.scale = loc, rot, scl


def pose_error(arm, target):
    """Deviation of the current pose from the target, in metres. Measures the bone tip
    as well, so orientation is checked and not just position."""
    bpy.context.view_layer.update()
    worst, where = 0.0, None
    for pb in arm.pose.bones:
        got, want = pb.matrix, target[pb.bone.name]
        tip = Vector((0, pb.bone.length, 0))
        d = max((got.translation - want.translation).length,
                ((got @ tip) - (want @ tip)).length)
        if d > worst:
            worst, where = d, pb.bone.name
    return worst, where


def scale_world(world, k):
    """Scale captured matrices by k: positions scale, orientation is untouched."""
    out = {}
    for name, frames in world.items():
        out[name] = {}
        for f, row in frames.items():
            new = {}
            for bone, m in row.items():
                loc, rot, scl = m.decompose()
                new[bone] = Matrix.LocRotScale(loc * k, rot, scl)
            out[name][f] = new
    return out


def mesh_bounds(meshes):
    dg = bpy.context.evaluated_depsgraph_get()
    co = []
    for m in meshes:
        ev = m.evaluated_get(dg)
        co += [ev.matrix_world @ v.co for v in ev.data.vertices]
    lo = Vector((min(c.x for c in co), min(c.y for c in co), min(c.z for c in co)))
    hi = Vector((max(c.x for c in co), max(c.y for c in co), max(c.z for c in co)))
    return lo, hi


def min_alpha(img):
    """Smallest alpha value in the image, or None if it has no alpha data.

    img.pixels[3::4] would copy the whole float buffer through Python - several seconds
    for a 2K texture. foreach_get fills a numpy buffer in one C-level call instead.
    """
    if img.channels != 4 or not img.has_data:
        return None
    try:
        import numpy as np
    except ImportError:                                   # pragma: no cover
        return min(img.pixels[3::4])
    buf = np.empty(img.size[0] * img.size[1] * 4, dtype=np.float32)
    img.pixels.foreach_get(buf)
    return float(buf[3::4].min())


# --------------------------------------------------------------------------- main

def main():
    check_blender_version()
    args = parse_args()
    src = os.path.abspath(args.src)
    if not os.path.isdir(src):
        raise BuildError(f"--src folder does not exist: {src}")
    out = os.path.abspath(args.out) if args.out else os.path.join(src, "Character.glb")
    name_map = (lambda n: n) if args.keep_prefix else sanitize

    # ---------------------------------------------------------------- 1. pick the files
    all_fbx = sorted(f for f in os.listdir(src) if f.lower().endswith(".fbx"))
    if not all_fbx:
        raise BuildError(f"no .fbx files in {src}")

    skin_file, skin_name = (split_spec(args.skin) if args.skin else (None, None))
    if skin_file is None:
        # the skinned file is the one that contains a mesh
        for f in all_fbx:
            bpy.ops.wm.read_factory_settings(use_empty=True)
            objs = import_fbx(os.path.join(src, f))
            if any(o.type == 'MESH' for o in objs):
                skin_file = f
                skin_name = os.path.splitext(f)[0]
                break
        if skin_file is None:
            raise BuildError("no FBX contains a mesh - cannot find the skinned file")
        log(f"skinned file detected automatically: {skin_file}")

    if args.anim:
        anim_specs = [split_spec(s) for s in args.anim]
    else:
        anim_specs = [(f, os.path.splitext(f)[0]) for f in all_fbx if f != skin_file]
    for f, _ in anim_specs + [(skin_file, None)]:
        if not os.path.isfile(os.path.join(src, f)):
            raise BuildError(f"file not found: {os.path.join(src, f)}")

    names = [n for _, n in anim_specs]
    if len(set(names)) != len(names):
        raise BuildError(f"animation names are not unique: {names}")

    log(f"skin : {skin_file}")
    log(f"anims: {[f'{f} -> {n}' for f, n in anim_specs] or '(none)'}")

    # ---------------------------------------------------------------- 2. the skinned rig
    bpy.ops.wm.read_factory_settings(use_empty=True)
    scn = bpy.context.scene
    import_fbx(os.path.join(src, skin_file))

    meshes = [o for o in scn.objects if o.type == 'MESH']
    arms = [o for o in scn.objects if o.type == 'ARMATURE']
    if not meshes:
        raise BuildError(f"{skin_file} contains no mesh - this is not the skinned file")
    if len(arms) != 1:
        raise BuildError(f"{skin_file} contains {len(arms)} armatures, expected 1")
    arm = arms[0]
    arm.name = arm.data.name = "Armature"
    if args.fps:
        scn.render.fps = args.fps
    log(f"skinned rig: {len(arm.data.bones)} bones, {len(meshes)} mesh(es), "
        f"{sum(len(m.data.vertices) for m in meshes)} verts, {scn.render.fps} fps")

    for m in meshes:
        if not any(mo.type == 'ARMATURE' and mo.object == arm for mo in m.modifiers):
            raise BuildError(f"mesh '{m.name}' is not bound to the armature")

    skin_act = arm.animation_data.action if arm.animation_data else None

    # The skinned file's own action has to be captured HERE, before the bones are
    # renamed. Renaming only repairs the fcurve paths of the action that is assigned at
    # the time, and step 3 has to detach it first (see the comment there). Captured
    # afterwards it would still load, drive nothing, and silently sample the rest pose -
    # a Mixamo character downloaded 'With Skin' together with an animation would arrive
    # in Godot as an empty animation. name_map produces the same keys either way.
    WORLD, RANGES = {}, {}
    if skin_act is not None:
        _a, _b = int(skin_act.frame_range[0]), int(skin_act.frame_range[1])
        WORLD[skin_name] = capture(arm, skin_act, range(_a, _b + 1), name_map)
        RANGES[skin_name] = (_a, _b)
        log(f"captured '{skin_name}' (the skinned file's own action) frames {_a}..{_b}")
    else:
        log(f"{skin_file} contains no action (normal - bind pose only)")

    # ---------------------------------------------------------------- 3. clean bone names
    if not args.keep_prefix:
        if arm.animation_data:
            arm.animation_data.action = None      # never rename while an action is assigned
        pending = {b.name: sanitize(b.name) for b in arm.data.bones}
        if len(set(pending.values())) != len(pending):
            raise BuildError("bone names collide after sanitizing")
        for old, new in pending.items():
            if old != new:
                arm.data.bones[old].name = new       # vertex groups follow automatically
        bone_names = {b.name for b in arm.data.bones}
        for m in meshes:
            dangling = [vg.name for vg in m.vertex_groups if vg.name not in bone_names]
            if dangling:
                raise BuildError(f"unmatched vertex groups on '{m.name}': {dangling}")
        log(f"bone names sanitized ({sum(1 for a, b in pending.items() if a != b)} changed)")

    BONES = {b.name for b in arm.data.bones}
    LENS = {b.name: b.length for b in arm.data.bones}
    OBJ_MW = arm.matrix_world.copy()

    roots = [b.name for b in arm.data.bones if b.parent is None]
    root_bone = args.root_bone or roots[0]
    if root_bone not in BONES:
        raise BuildError(f"--root-bone '{root_bone}' is not in the skeleton")
    if len(roots) > 1:
        log(f"WARNING: multiple root bones {roots}, using '{root_bone}' for root motion")

    # ---------------------------------------------------------------- 4. capture animations
    REST_TARGET = None
    for fname, aname in anim_specs:
        objs = import_fbx(os.path.join(src, fname))
        srcs = [o for o in objs if o.type == 'ARMATURE']
        if len(srcs) != 1:
            raise BuildError(f"{fname}: found {len(srcs)} armatures, expected 1")
        s_arm = srcs[0]

        s_names = {name_map(b.name) for b in s_arm.data.bones}
        missing = BONES - s_names
        if missing:
            raise BuildError(f"{fname}: these bones of the skinned rig are missing: "
                             f"{sorted(missing)}")
        if s_names - BONES:
            log(f"  note: {fname} has extra bones, ignoring: {sorted(s_names - BONES)}")

        # Same skeleton? If bone LENGTHS do not match this is a real retargeting job,
        # which is not what this script does.
        worst_rel, worst_bone = 0.0, None
        for b in s_arm.data.bones:
            n = name_map(b.name)
            if n in LENS:
                ref = max(LENS[n], b.length, 1e-9)
                r = abs(b.length - LENS[n]) / ref
                if r > worst_rel:
                    worst_rel, worst_bone = r, n
        if worst_rel > 0.01:
            raise BuildError(
                f"{fname}: bone lengths differ from the skinned rig by {worst_rel*100:.1f}% "
                f"('{worst_bone}'). This is a different skeleton - it needs real "
                f"retargeting, which this script does not do.")

        if s_arm.matrix_world != OBJ_MW:
            log(f"  WARNING: {fname} has a different object matrix than the skinned file "
                f"(harmless, everything is solved in world space)")

        # the animation rig's REST pose (used as the rest target, and for diagnostics)
        s_act = s_arm.animation_data.action if s_arm.animation_data else None
        if s_arm.animation_data:
            s_arm.animation_data.action = None
        clear_pose(s_arm)
        rest_here = capture(s_arm, None, [scn.frame_current], name_map)[scn.frame_current]
        if REST_TARGET is None:
            REST_TARGET = rest_here
        else:
            gap = max((rest_here[b].translation - REST_TARGET[b].translation).length
                      for b in BONES)
            if gap > 1e-3:
                log(f"  WARNING: {fname} rest pose differs from the other animation files "
                    f"by {gap:.3f} m")

        if s_act is None:
            raise BuildError(f"{fname} contains no action")
        a, b = int(s_act.frame_range[0]), int(s_act.frame_range[1])
        WORLD[aname] = capture(s_arm, s_act, range(a, b + 1), name_map)
        RANGES[aname] = (a, b)

        # how far is this from the skinned rig's rest pose? (the crux of the whole problem)
        skin_rest = {b.name: (OBJ_MW @ b.matrix_local).translation for b in arm.data.bones}
        gap = max((rest_here[n].translation - skin_rest[n]).length for n in BONES)
        log(f"captured '{aname}' frames {a}..{b} ({b - a + 1} frames) | "
            f"rest gap vs skinned rig: {gap:.3f} m")

        bpy.data.actions.remove(s_act)
        for o in objs:
            bpy.data.objects.remove(o, do_unlink=True)

    if not WORLD:
        raise BuildError("no animation was captured")

    if arm.animation_data:
        arm.animation_data.action = None
    for a in list(bpy.data.actions):
        bpy.data.actions.remove(a)      # no action may survive into the scale change
    clear_pose(arm)

    # ---------------------------------------------------------------- 5. cm -> m
    # NOTE: do NOT use Armature.data.transform() - it changes rest data while the
    # evaluated pose goes stale. The operator does the right thing.
    parenting = [(m, m.matrix_world.copy()) for m in meshes]
    for m, mw in parenting:
        m.parent = None
        m.matrix_world = mw
    bpy.ops.object.select_all(action='DESELECT')
    arm.select_set(True)
    for m in meshes:
        m.select_set(True)
    bpy.context.view_layer.objects.active = arm
    bpy.ops.object.transform_apply(location=False, rotation=True, scale=True)
    for m, _ in parenting:
        m.parent = arm
        m.matrix_parent_inverse = arm.matrix_world.inverted()
    bpy.context.view_layer.update()
    if arm.matrix_world != Matrix.Identity(4):
        raise BuildError(f"armature object matrix did not become identity:\n{arm.matrix_world}")
    log("armature + meshes converted to metres, object matrix is identity")

    # ---------------------------------------------------------------- 6. rest pose
    rest_target = None
    if args.rest == "bind":
        log("rest pose: keeping the bind pose (--rest bind)")
    elif args.rest == "auto":
        if REST_TARGET is None:
            log("rest pose: no animation file, keeping the bind pose")
        else:
            rest_target = REST_TARGET
            log("rest pose: applying the animation rigs' rest pose")
    else:
        if ":" not in args.rest:
            raise BuildError("--rest format: 'auto' | 'bind' | '<AnimName>:<frame>'")
        an, fr = args.rest.rsplit(":", 1)
        if an not in WORLD:
            raise BuildError(f"--rest: no animation called '{an}'. Available: {sorted(WORLD)}")
        if int(fr) not in WORLD[an]:
            raise BuildError(f"--rest: '{an}' has no frame {fr} {RANGES[an]}")
        rest_target = WORLD[an][int(fr)]
        log(f"rest pose: applying frame {fr} of '{an}'")

    if rest_target is not None:
        blocked = [m.name for m in meshes if m.data.shape_keys]
        if blocked:
            raise BuildError(
                f"mesh(es) {blocked} have shape keys - the rest pose cannot be changed. "
                f"Run with --rest bind.")

        apply_basis(arm, solve_basis(rest_target, arm))
        err, where = pose_error(arm, rest_target)
        log(f"  solver check: {err*1000:.5f} mm (worst: {where})")
        if err > args.tolerance:
            raise BuildError(f"solver could not reach the target rest pose ({err} m)")

        for m in meshes:                                  # bake the deformation into the mesh
            mod = next(x for x in m.modifiers if x.type == 'ARMATURE')
            bpy.ops.object.select_all(action='DESELECT')
            m.select_set(True)
            bpy.context.view_layer.objects.active = m
            bpy.ops.object.modifier_copy(modifier=mod.name)     # leave a live copy behind
            bpy.ops.object.modifier_apply(modifier=mod.name)
            if [x.type for x in m.modifiers].count('ARMATURE') != 1:
                raise BuildError(f"unexpected modifiers on '{m.name}': "
                                 f"{[x.type for x in m.modifiers]}")

        bpy.ops.object.select_all(action='DESELECT')
        arm.select_set(True)
        bpy.context.view_layer.objects.active = arm
        bpy.ops.object.mode_set(mode='POSE')
        bpy.ops.pose.select_all(action='SELECT')
        bpy.ops.pose.armature_apply()
        bpy.ops.object.mode_set(mode='OBJECT')
        clear_pose(arm)
        log("  new rest pose applied")

    # ---------------------------------------------------------------- 7. height normalization
    lo, hi = mesh_bounds(meshes)
    natural_h = hi.z - lo.z
    k = 1.0
    if args.target_height:
        k = args.target_height / natural_h
        arm.scale = (k, k, k)
        bpy.context.view_layer.update()
        parenting = [(m, m.matrix_world.copy()) for m in meshes]
        for m, mw in parenting:
            m.parent = None
            m.matrix_world = mw
        bpy.ops.object.select_all(action='DESELECT')
        arm.select_set(True)
        for m in meshes:
            m.select_set(True)
        bpy.context.view_layer.objects.active = arm
        bpy.ops.object.transform_apply(location=False, rotation=True, scale=True)
        for m, _ in parenting:
            m.parent = arm
            m.matrix_parent_inverse = arm.matrix_world.inverted()
        bpy.context.view_layer.update()
        WORLD = scale_world(WORLD, k)
        log(f"height {natural_h:.3f} m -> {args.target_height:.3f} m (x{k:.5f})")

    # ---------------------------------------------------------------- 8. in-place
    for name in args.in_place:
        if name not in WORLD:
            raise BuildError(f"--in-place: no animation called '{name}'. "
                             f"Available: {sorted(WORLD)}")
        frames = sorted(WORLD[name])
        ref = WORLD[name][frames[0]][root_bone].translation
        moved = 0.0
        for f in frames:
            off = WORLD[name][f][root_bone].translation - ref
            off = Vector((off.x, off.y, 0.0))            # vertical bounce is preserved
            moved = max(moved, off.length)
            for bone, m in WORLD[name][f].items():
                loc, rot, scl = m.decompose()
                WORLD[name][f][bone] = Matrix.LocRotScale(loc - off, rot, scl)
        log(f"in-place '{name}': removed {moved:.3f} m of horizontal root motion")

    # ---------------------------------------------------------------- 9. bake
    for pb in arm.pose.bones:
        pb.rotation_mode = 'QUATERNION'
    for name in WORLD:
        act = bpy.data.actions.new(name)
        act.use_fake_user = True
        assign_action(arm, act)
        for f in sorted(WORLD[name]):
            apply_basis(arm, solve_basis(WORLD[name][f], arm))
            for pb in arm.pose.bones:
                pb.keyframe_insert("location", frame=f)
                pb.keyframe_insert("rotation_quaternion", frame=f)
                pb.keyframe_insert("scale", frame=f)
        log(f"baked '{name}': {len(WORLD[name])} frames x {len(arm.pose.bones)} bones")

    # ---------------------------------------------------------------- 10. VERIFICATION
    log("-" * 62)
    worst_all = 0.0
    for name in WORLD:
        assign_action(arm, bpy.data.actions[name])
        worst, where, wf = 0.0, None, None
        for f in sorted(WORLD[name]):
            scn.frame_set(f)
            e, w = pose_error(arm, WORLD[name][f])
            if e > worst:
                worst, where, wf = e, w, f
        worst_all = max(worst_all, worst)
        log(f"'{name}': max deviation from source {worst*1000:.5f} mm (frame {wf}, {where})")
    if worst_all > args.tolerance:
        raise BuildError(f"baked animation does not match the source ({worst_all} m)")
    log(f"ALL ANIMATIONS REPRODUCE THE SOURCE RIG WITHIN {worst_all*1000:.5f} mm")
    log("-" * 62)

    # ---------------------------------------------------------------- 11. report
    if arm.animation_data:
        arm.animation_data.action = None
    clear_pose(arm)                                   # measure in the rest pose
    lo, hi = mesh_bounds(meshes)
    h = hi.z - lo.z
    log(f"height in rest pose {h:.3f} m | feet z={lo.z:+.3f} | width X {hi.x - lo.x:.3f} m")
    if not args.target_height:
        log(f"Godot Root Scale for 1.75 m = {1.75 / h:.4f}")
    for name in sorted(WORLD):
        ks = sorted(WORLD[name])
        d = WORLD[name][ks[-1]][root_bone].translation - WORLD[name][ks[0]][root_bone].translation
        rm = Vector((d.x, d.y)).length
        log(f"  {name:20s} {len(ks):4d} frames  {len(ks)/scn.render.fps:6.2f} s  "
            f"root motion {rm:6.3f} m" + ("  <-- not in place" if rm > 0.05 else ""))

    # ---------------------------------------------------------------- 12. materials
    for m in bpy.data.materials:
        # Material.use_nodes is going away in Blender 6.0; test for node_tree instead
        nt = getattr(m, "node_tree", None)
        if nt is None:
            continue
        bsdf = next((n for n in nt.nodes if n.type == 'BSDF_PRINCIPLED'), None)
        if bsdf and bsdf.inputs['Alpha'].is_linked:
            imgs = [l.from_node.image for l in bsdf.inputs['Alpha'].links
                    if l.from_node.type == 'TEX_IMAGE' and l.from_node.image]
            alphas = [min_alpha(i) for i in imgs]
            if imgs and all(a is not None and a >= 0.999 for a in alphas):
                for l in list(bsdf.inputs['Alpha'].links):
                    nt.links.remove(l)
                bsdf.inputs['Alpha'].default_value = 1.0
                for attr, val in (("blend_method", 'OPAQUE'), ("surface_render_method", 'DITHERED')):
                    if hasattr(m, attr):
                        setattr(m, attr, val)
                log(f"material '{m.name}': alpha map is fully opaque -> forced to OPAQUE")
            else:
                log(f"material '{m.name}': alpha map has real transparency, left alone")
    for img in list(bpy.data.images):
        if img.name != 'Render Result' and img.packed_file is None and img.has_data:
            img.pack()

    # ---------------------------------------------------------------- 13. export
    if arm.animation_data:
        arm.animation_data.action = None
    scn.frame_start = 1
    scn.frame_end = int(max(max(v) for v in WORLD.values()))

    desired = dict(
        filepath=out, export_format='GLB', use_selection=False,
        export_yup=True, export_apply=False,
        export_materials='EXPORT', export_image_format='AUTO',
        export_skins=True, export_def_bones=False, export_rest_position_armature=True,
        export_animations=True, export_animation_mode='ACTIONS',
        export_bake_animation=True, export_force_sampling=True,
        export_anim_slide_to_zero=True, export_optimize_animation_size=False,
        export_frame_range=False, export_reset_pose_bones=True, export_current_frame=False,
        export_extras=False, export_cameras=False, export_lights=False, export_morph=True,
    )
    valid = set(bpy.ops.export_scene.gltf.get_rna_type().properties.keys())
    dropped = [k for k in desired if k not in valid]
    if dropped:
        log("export options not present in this Blender version, skipped:", dropped)
    out_dir = os.path.dirname(out)
    if out_dir:
        os.makedirs(out_dir, exist_ok=True)
    bpy.ops.export_scene.gltf(**{k: v for k, v in desired.items() if k in valid})
    log(f"WROTE {out}  ({os.path.getsize(out)} bytes)")

    blend = args.blend or os.path.splitext(out)[0] + "_source.blend"
    bpy.ops.wm.save_as_mainfile(filepath=os.path.abspath(blend))
    log(f"WROTE {blend}")


if __name__ == "__main__":
    try:
        main()
    except BuildError as e:
        print(f"\n[build] ERROR: {e}\n", file=sys.stderr)
        sys.stdout.flush()
        sys.exit(1)
