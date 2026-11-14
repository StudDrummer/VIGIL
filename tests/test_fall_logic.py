"""FALL_V5 regression: every synthetic scenario (falls forward/sideways/toward the
camera, slump from chair to floor, slow collapse; and walking, sitting, bending,
crouching, kneeling, lying on the bed, sitting on the floor on purpose) at 5 and
2 frames a second, both directions, must be decided right."""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "tools"))
os.environ.setdefault("FALL_EVAL_FLOOR_S", "60")
import fall_eval  # noqa: E402

ok, n, rows = fall_eval.synthetic(quiet=True)
bad = [r for r in rows if not r[6]]
for fps, name, side, is_fall, fa, fl, good in bad:
    print("WRONG %2.0f fps %s %s fall=%s floor=%s" % (fps, name, side, fa, fl))
print("%d/%d right" % (ok, n))
assert not bad
print("ALL PASS")
