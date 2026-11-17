# VIGIL box

`vigil_box.py` builds a printable case for the relay box (Raspberry Pi Zero 2 W):
88 x 58 x 24 mm closed, two parts, no supports.

```
pip install manifold3d trimesh
python3 hardware/vigil_box.py
```

It writes `vigil_box_base.stl` (print open side up), `vigil_box_lid.stl` (print top side
down) and `vigil_box_preview.stl` (assembled, lid lifted, board stand-in, for looking at).

Inside: the Pi on four M2.5 posts (thread-forming screws), the microSD reachable from the
end, power and a second micro-USB (for an Ethernet adapter) out the front, vents on the
back, a 0.4 mm light window over the board's LED, keyholes underneath for wall mounting.

Port positions come from the Pi Zero's published drawing and have not yet been checked
against a printed part. Print one, fit a real board, then adjust the numbers at the top
of the script.
