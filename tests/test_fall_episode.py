"""FALL_EPISODE_V1: one fall, one alert. Replays the Nov 16 storm (someone on the floor
who keeps shifting, with the tracker handing out new IDs) through the real alert path,
and checks that getting up, or a fall somewhere else, still alerts."""
import os
import sys
import types

import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "tools"))
sys.path.insert(0, os.path.join(HERE, "..", "jetson-backend"))
sys.modules.setdefault("ultralytics", types.SimpleNamespace(YOLO=object))
import activity_monitor as am  # noqa: E402
import fall_sim  # noqa: E402

SHAPE = (1080, 1920)
CLOCK = [1e6]
am.time = types.SimpleNamespace(time=lambda: CLOCK[0], sleep=lambda s: None, monotonic=lambda: CLOCK[0])


class Client:
    def __init__(self): self.alerts = []
    def alert_fall(self, hip, torso, message="Fall detected by camera"): self.alerts.append((CLOCK[0], message))
    def _post(self, *a, **k): pass


def reset():
    am._FALL_BY_TID.clear(); am._TID_SEEN.clear(); am._FALL_EPISODES[:] = []
    am._FALL_SOLO[0] = None; am._FALL_SOLO[1] = 0.0; am._FALL_LAST_ALERT[0] = 0.0
    CLOCK[0] += 10000


def play(client, steps, tid=1, x0=900.0, fps=5.0, seed=0, new_id_every=None, extra=None):
    """Feed one person's movement through the camera loop's fall path. extra: another
    person's (kp) standing still elsewhere, to make it a two-person room."""
    t0 = CLOCK[0]
    for t, kp in fall_sim.sequence(steps, fps=fps, seed=seed, x0=x0):
        CLOCK[0] = t0 + t
        this = tid + (int(t // new_id_every) if new_id_every else 0)
        persons, tids = [kp], [this]
        if extra is not None:
            persons.append(extra); tids.append(999)
        am._fall_per_person(persons, tids, SHAPE, client)
    CLOCK[0] = t0 + t + 1.0 / fps


# an hour of someone sitting (no bed or couch area drawn there) who keeps slumping
# sideways and sitting back up: what set off an alert a minute on Nov 16
RESTLESS = [("stand", 2), ("sit_chair", 2), ("sit_chair", 5)] + [("lie", 0.8), ("lie", 45), ("sit_chair", 1.5), ("sit_chair", 12)] * 60


def test_storm_is_one_alert():
    am.FALL_EPISODE = False; reset(); old = Client()
    play(old, RESTLESS, new_id_every=30.0)
    am.FALL_EPISODE = True; reset(); new = Client()
    play(new, RESTLESS, new_id_every=30.0)
    hours = (new.alerts[-1][0] - new.alerts[0][0]) / 3600.0 if len(new.alerts) > 1 else 0
    print("restless hour on the floor: %d alerts before, %d now" % (len(old.alerts), len(new.alerts)))
    assert len(old.alerts) >= 10, "the test no longer reproduces the storm (%d)" % len(old.alerts)
    assert 1 <= len(new.alerts) <= 3, new.alerts            # the fall, then at most one every 30 minutes
    assert all(b[0] - a[0] >= am.FALL_REARM_MAX_S - 1 for a, b in zip(new.alerts, new.alerts[1:])), hours


def test_getting_up_and_falling_again_alerts_again():
    reset(); c = Client()
    play(c, [("stand", 2), ("lie", 0.8), ("lie", 15)])
    assert len(c.alerts) == 1, c.alerts
    play(c, [("lie", 1), ("kneel", 2), ("stand", 2), ("stand", 8)])          # helped up, on their feet
    play(c, [("stand", 2), ("lie", 0.8), ("lie", 15)])                        # and goes down again
    assert len(c.alerts) == 2, c.alerts


def test_sitting_up_is_not_getting_up():
    reset(); c = Client()
    play(c, [("stand", 2), ("lie", 0.8), ("lie", 15), ("sit_floor", 2), ("sit_floor", 30), ("lie", 1.5), ("lie", 90)])
    assert len(c.alerts) == 1, c.alerts


def test_second_person_elsewhere_still_alerts():
    reset(); c = Client()
    play(c, [("stand", 2), ("lie", 0.8), ("lie", 15)], tid=1, x0=500.0)
    assert len(c.alerts) == 1
    down = fall_sim.sequence([("lie", 1)], x0=500.0)[-1][1]                  # the first person stays where they fell
    CLOCK[0] += 40
    t0 = CLOCK[0]
    for t, kp in fall_sim.sequence([("stand", 2), ("lie", 0.8), ("lie", 15)], x0=1500.0, seed=3):
        CLOCK[0] = t0 + t
        am._fall_per_person([down + np.random.default_rng(int(t * 10)).normal(0, 2, down.shape) * [1, 1, 0], kp], [1, 2], SHAPE, c)
    assert len(c.alerts) == 2, c.alerts


def test_someone_walking_past_does_not_reopen_it():
    reset(); c = Client()
    play(c, [("stand", 2), ("lie", 0.8), ("lie", 15)], tid=1, x0=500.0)
    walker = fall_sim.sequence([("stand", 1)], x0=1500.0)[-1][1]             # another person on their feet across the room
    play(c, [("sit_floor", 2), ("sit_floor", 6), ("lie", 2), ("lie", 70)] * 6, tid=1, x0=500.0, new_id_every=30.0, extra=walker)
    assert len(c.alerts) == 1, c.alerts


if __name__ == "__main__":
    test_storm_is_one_alert(); test_getting_up_and_falling_again_alerts_again(); test_sitting_up_is_not_getting_up()
    test_second_person_elsewhere_still_alerts(); test_someone_walking_past_does_not_reopen_it()
    print("ALL PASS")
