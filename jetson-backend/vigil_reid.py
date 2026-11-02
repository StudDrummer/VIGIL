"""
VIGIL re-identification (v6): a learned appearance model plus a memory.

  Embedder  turns a crop of one person into a 384-number "appearance
            fingerprint" using DINOv2 ViT-S/14 (Meta, self-supervised on
            142M images; strong at telling instances apart). Falls back to
            a torchvision MobileNetV3 if DINOv2 cannot be loaded.
  Gallery   remembers fingerprints per person on disk (reid_gallery.npz).
            Confirmations and corrections are pinned and never forgotten,
            so a corrected outfit is recognised next time, even another day.
            Accept thresholds are learned from the gallery itself: how
            similar a person is to themselves vs. to everyone else.

Privacy: fingerprints are numbers computed from a body crop on the device.
No image is kept, and the model does not use or isolate the face.
"""
import json
import os
import threading
import time

import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
os.environ.setdefault("TORCH_HOME", os.path.join(HERE, "torch_cache"))   # model files live with VIGIL
GALLERY_FILE = os.path.join(HERE, "reid_gallery.npz")
BACKEND_FILE = os.path.join(HERE, "reid_backend.json")

MAX_ENTRIES = 120         # per person
MAX_PINNED = 80           # caregiver-verified looks; the most redundant goes first, not the oldest
DIVERSITY = 0.95          # do not store a fingerprint almost identical to one we have
VERSION = "REID_V62"
UNSURE_FLOOR = 0.60       # below this for everyone known: a genuinely new person
LEAD_FLOOR = 0.62         # a clear leader above this is accepted even under the strict bar
LEAD_GAP = 0.10           # ...when it beats everyone else by this much


class Embedder:
    """Lazily loads the model on first use. Never raises to the caller."""

    def __init__(self):
        self.model = None
        self.kind = None
        self.dev = None
        self.failed = False
        self.ms = None
        self._lock = threading.Lock()

    def load(self):
        if self.model is not None or self.failed:
            return self.model is not None
        with self._lock:
            if self.model is not None or self.failed:
                return self.model is not None
            try:
                import torch
                self.dev = "cuda" if torch.cuda.is_available() else "cpu"
                pref = "dinov2"
                try:
                    pref = json.load(open(BACKEND_FILE)).get("backend", "dinov2")
                except Exception:
                    pass
                m = None
                if pref == "dinov2":
                    try:
                        m = torch.hub.load("facebookresearch/dinov2", "dinov2_vits14", verbose=False)
                        self.kind = "dinov2"
                    except Exception as e:
                        print("[REID6] DINOv2 unavailable (%s); using MobileNetV3" % e, flush=True)
                if m is None:
                    import torchvision
                    w = torchvision.models.MobileNet_V3_Small_Weights.IMAGENET1K_V1
                    net = torchvision.models.mobilenet_v3_small(weights=w)
                    net.classifier = torch.nn.Identity()
                    m = net
                    self.kind = "mobilenet"
                m = m.eval().to(self.dev)
                if self.dev == "cuda":
                    m = m.half()
                self.model = m
                print("[REID6] model ready: %s on %s" % (self.kind, self.dev), flush=True)
                return True
            except Exception as e:
                self.failed = True
                print("[REID6] model failed to load: %s" % e, flush=True)
                return False

    def embed(self, crops):
        """crops: list of RGB uint8 arrays (H x W x 3), any size.
        Returns an (n x d) array of unit vectors, or None."""
        if not crops or self.model is None:
            return None          # still loading in the background; never stall the camera
        try:
            import torch
            import cv2
            H, W = 224, 112
            batch = np.stack([cv2.resize(c, (W, H), interpolation=cv2.INTER_AREA) for c in crops]).astype(np.float32) / 255.0
            batch = (batch - np.array([0.485, 0.456, 0.406], np.float32)) / np.array([0.229, 0.224, 0.225], np.float32)
            t0 = time.time()
            with torch.no_grad():
                x = torch.from_numpy(batch.transpose(0, 3, 1, 2)).to(self.dev)
                if self.dev == "cuda":
                    x = x.half()
                f = self.model(x)
                if isinstance(f, (tuple, list)):
                    f = f[0]
                f = torch.nn.functional.normalize(f.float(), dim=1).cpu().numpy()
            ms = (time.time() - t0) * 1000.0
            self.ms = ms if self.ms is None else 0.9 * self.ms + 0.1 * ms
            return f
        except Exception as e:
            print("[REID6] embed error: %s" % e, flush=True)
            return None


def crop_person(frame_bgr, kp, conf=0.3):
    """Body crop from pose keypoints, padded; RGB. None if too small."""
    try:
        k = np.asarray(kp, dtype=float)
        pts = k[k[:, 2] >= conf]
        if len(pts) < 6:
            return None
        x1, y1 = pts[:, 0].min(), pts[:, 1].min()
        x2, y2 = pts[:, 0].max(), pts[:, 1].max()
        w, h = x2 - x1, y2 - y1
        if h < 60 or w < 20:
            return None
        x1 -= 0.15 * w; x2 += 0.15 * w
        y1 -= 0.12 * h; y2 += 0.08 * h
        fh, fw = frame_bgr.shape[:2]
        x1, y1 = max(0, int(x1)), max(0, int(y1))
        x2, y2 = min(fw, int(x2)), min(fh, int(y2))
        if x2 - x1 < 20 or y2 - y1 < 60:
            return None
        return frame_bgr[y1:y2, x1:x2, ::-1].copy()
    except Exception:
        return None


class Gallery:
    """Per-person memory of appearance fingerprints."""

    def __init__(self, path=GALLERY_FILE):
        self.path = path
        self.vecs = {}       # pid -> (n x d) array
        self.pinned = {}     # pid -> bool array
        self.dirty = False
        self.saved = 0.0
        self.th = None       # (accept, margin), learned
        self.th_t = 0.0
        self.named = set()   # person ids with a name; set by person_hook
        # ADAPT_V1: a household-specific model learned from caregiver-verified
        # fingerprints. It is used only while it measurably beats the plain
        # comparison on held-out verified samples (see learn()).
        self.proj = None     # (d x d) transform, or None
        self.cal = (1.0, 0.0)  # maps transformed similarity back to the usual scale
        self.adapt_info = {}
        self._pinned_at_learn = -1
        self._learn_t = 0.0
        self._learning = False
        self.last_best = None
        self.last_top = []
        self.last_kind = None
        self.load()

    # ---------------------------------------------------------- storage
    def load(self):
        try:
            z = np.load(self.path, allow_pickle=False)
            meta = json.loads(str(z["meta"]))
            for i, pid in enumerate(meta["pids"]):
                self.vecs[pid] = z["v%d" % i].astype(np.float32)
                self.pinned[pid] = z["p%d" % i].astype(bool)
            if "proj" in z.files:
                self.proj = z["proj"].astype(np.float32)
                self.cal = tuple(meta.get("cal", (1.0, 0.0)))
                self.adapt_info = meta.get("adapt", {})
            print("[REID6] gallery loaded: %s" % {p: len(v) for p, v in self.vecs.items()}, flush=True)
        except Exception:
            pass

    def save(self, force=False):
        now = time.time()
        if not (self.dirty and (force or now - self.saved > 60)):
            return
        try:
            pids = [p for p in self.vecs if len(self.vecs[p])]
            meta = {"pids": pids}
            if self.proj is not None:
                meta["cal"] = list(self.cal)
                meta["adapt"] = self.adapt_info
            arrs = {"meta": np.array(json.dumps(meta))}
            if self.proj is not None:
                arrs["proj"] = self.proj.astype(np.float32)
            for i, p in enumerate(pids):
                arrs["v%d" % i] = self.vecs[p].astype(np.float16)
                arrs["p%d" % i] = self.pinned[p].astype(np.uint8)
            tmp = self.path + ".tmp.npz"
            np.savez_compressed(tmp, **arrs)
            os.replace(tmp, self.path)
            self.dirty = False
            self.saved = now
            self.maybe_learn()
        except Exception as e:
            print("[REID6] save error: %s" % e, flush=True)

    # ---------------------------------------------------------- edits
    def add(self, pid, vecs, pin=False):
        if pid is None or vecs is None or len(vecs) == 0:
            return 0
        vecs = np.asarray(vecs, dtype=np.float32)
        cur = self.vecs.get(pid)
        curp = self.pinned.get(pid)
        added = 0
        for v in vecs:
            if cur is not None and len(cur):
                if float((cur @ v).max()) >= DIVERSITY and not pin:
                    continue
                cur = np.vstack([cur, v[None]])
                curp = np.append(curp, pin)
            else:
                cur = v[None].copy()
                curp = np.array([pin])
            added += 1
        if cur is None:
            return 0
        # cap: keep the most varied pinned looks (drop the most redundant one
        # at a time, so an old outfit is not forgotten just for being old),
        # then the newest unpinned.
        idx = np.arange(len(cur))
        pin_idx = idx[curp]
        while len(pin_idx) > MAX_PINNED:
            pv = cur[pin_idx]
            sims = pv @ pv.T
            np.fill_diagonal(sims, -1.0)
            pin_idx = np.delete(pin_idx, int(np.argmax(sims.max(axis=1))))
        free_idx = idx[~curp][-(MAX_ENTRIES - len(pin_idx)):] if MAX_ENTRIES > len(pin_idx) else idx[:0]
        keep = np.sort(np.concatenate([pin_idx, free_idx]))
        self.vecs[pid] = cur[keep]
        self.pinned[pid] = curp[keep]
        if added:
            self.dirty = True
        return added

    def remove_recent(self, pid, vecs, thresh=0.97):
        """Remove entries (unpinned) that match fingerprints wrongly added to pid."""
        cur = self.vecs.get(pid)
        if cur is None or vecs is None or len(vecs) == 0:
            return
        sims = (cur @ np.asarray(vecs, np.float32).T).max(axis=1)
        keep = (sims < thresh) | self.pinned[pid]
        self.vecs[pid] = cur[keep]
        self.pinned[pid] = self.pinned[pid][keep]
        self.dirty = True

    def merge(self, src, dst, pin_all=True):
        """'Same as' in the app: the caregiver confirmed these are one person."""
        if src in self.vecs:
            v, p = self.vecs.pop(src), self.pinned.pop(src)
            if len(v):
                self.add(dst, v, pin=pin_all)
            self.dirty = True

    def others_best(self, vecs, pid):
        """Highest score of these fingerprints against any OTHER named person."""
        best = None
        for q in list(self.vecs):
            if q == pid or (self.named and q not in self.named):
                continue
            s = self.score(vecs, q)
            if s is not None and (best is None or s > best):
                best = s
        return best

    def exclusive_rows(self, vecs, pid, gap=0.08):
        """REID_V62: only fingerprints that are clearly this person's (better
        than everyone else by `gap`) may be learned automatically. This keeps
        a look shared by two people out of both memories."""
        vecs = np.asarray(vecs, np.float32)
        if vecs.ndim == 1:
            vecs = vecs[None]
        keep = []
        for v in vecs:
            own = self.score(v[None], pid)
            ob = self.others_best(v[None], pid)
            if own is None or ob is None or own - ob >= gap:
                keep.append(v)
        return np.array(keep, np.float32) if keep else vecs[:0]

    # ---------------------------------------------------------- ADAPT_V1
    def maybe_learn(self, force=False):
        """Relearn after every 5 new verified looks, or every 6 hours."""
        npin = int(sum(int(p.sum()) for p in self.pinned.values()))
        due = force or npin >= self._pinned_at_learn + 5 or time.time() - self._learn_t > 6 * 3600
        if not due or self._learning:
            return
        self._learning = True
        self._pinned_at_learn = npin
        self._learn_t = time.time()
        threading.Thread(target=self._learn_safe, daemon=True, name="reid-learn").start()

    def _learn_safe(self):
        try:
            info = self.learn()
            if info:
                print("[ADAPT] %s" % json.dumps(info), flush=True)
                self.save(force=True) if info.get("adopted") else None
                try:
                    with open(os.path.join(HERE, "identity_model.json"), "w") as f:
                        json.dump(info, f)
                except OSError:
                    pass
        except Exception as e:
            print("[ADAPT] learn error: %s" % e, flush=True)
        finally:
            self._learning = False

    def learn(self, min_people=2, min_verified=6, shrink=0.35):
        """Learn which differences in a fingerprint matter in THIS household.

        Training data: caregiver-verified fingerprints (confirmations and
        corrections) of named people. The model is a within-person whitening
        transform: directions in which one person varies a lot (lighting,
        pose, sitting vs standing) count less; directions that separate people
        count more. It is adopted only if, on verified samples held out from
        training, it names people more accurately than the plain comparison.
        """
        people = [p for p in self.vecs if (not self.named or p in self.named)
                  and int(self.pinned[p].sum()) >= min_verified]
        if len(people) < min_people:
            return {"adopted": False, "reason": "need %d people with %d verified looks" % (min_people, min_verified)}
        X = {p: self.vecs[p][self.pinned[p]].astype(np.float32) for p in people}
        rng = np.random.default_rng(0)
        folds = {p: rng.permutation(len(X[p])) % 3 for p in people}

        def fit(train):
            d = next(iter(train.values())).shape[1]
            Sw = np.zeros((d, d), np.float64)
            n = 0
            for v in train.values():
                c = v - v.mean(axis=0, keepdims=True)
                Sw += c.T @ c
                n += len(v)
            Sw /= max(n, 1)
            Sw = (1 - shrink) * Sw + shrink * (np.trace(Sw) / d) * np.eye(d)
            w, U = np.linalg.eigh(Sw)
            return (U @ np.diag(1.0 / np.sqrt(np.maximum(w, 1e-8))) @ U.T).astype(np.float32)

        def accuracy(P, train, test):
            def tx(x):
                if P is None:
                    return x
                y = x @ P
                return y / np.maximum(np.linalg.norm(y, axis=1, keepdims=True), 1e-6)
            ok = tot = 0
            per = {}
            for p, tv in test.items():
                good = 0
                for v in tv:
                    best, bp = -9.0, None
                    for q, gv in train.items():
                        s = np.sort(tx(gv) @ tx(v[None]).T, axis=0)[-3:].mean()
                        if s > best:
                            best, bp = s, q
                    good += int(bp == p)
                per[p] = good / max(len(tv), 1)
                ok += good
                tot += len(tv)
            return ok / max(tot, 1), per

        base_acc, new_acc, base_per, new_per = [], [], {p: [] for p in people}, {p: [] for p in people}
        for f in range(3):
            train = {p: X[p][folds[p] != f] for p in people}
            test = {p: X[p][folds[p] == f] for p in people}
            if min(len(v) for v in train.values()) < 3:
                continue
            P = fit(train)
            a0, p0 = accuracy(None, train, test)
            a1, p1 = accuracy(P, train, test)
            base_acc.append(a0); new_acc.append(a1)
            for p in people:
                base_per[p].append(p0[p]); new_per[p].append(p1[p])
        if not base_acc:
            return {"adopted": False, "reason": "not enough verified looks to test"}
        b, nw = float(np.mean(base_acc)), float(np.mean(new_acc))
        worst_drop = max(float(np.mean(base_per[p]) - np.mean(new_per[p])) for p in people)
        info = {"people": len(people), "verified": int(sum(len(v) for v in X.values())),
                "accuracy_before": round(b, 3), "accuracy_after": round(nw, 3),
                "worst_person_drop": round(worst_drop, 3), "at": time.strftime("%Y-%m-%d %H:%M")}
        if nw < b + 0.01 or worst_drop > 0.05:
            info["adopted"] = False
            info["reason"] = "did not beat the plain comparison on held-out looks"
            self.proj = None
            self.cal = (1.0, 0.0)
            self.adapt_info = info
            return info
        P = fit(X)
        # Calibrate so a "same person" and "different person" score land where
        # they did before; every threshold elsewhere keeps its meaning.
        def pair_sims(transform):
            gen, imp = [], []
            for p in people:
                a = X[p] if transform is None else self._norm(X[p] @ transform)
                g = a @ a.T
                gen.extend(g[np.triu_indices(len(a), 1)].tolist())
                for q in people:
                    if q <= p:
                        continue
                    bq = X[q] if transform is None else self._norm(X[q] @ transform)
                    imp.extend((a @ bq.T).ravel().tolist())
            return np.median(gen), np.median(imp)
        # Typical DINOv2 values for the same person (0.85) and different people
        # (0.45), the scale the accept/unsure bars were set on.
        g1, i1 = pair_sims(P)
        a_ = (0.85 - 0.45) / max(g1 - i1, 1e-3)
        c = 0.85 - a_ * g1
        self.proj, self.cal = P, (float(a_), float(c))
        self.th = None           # relearn thresholds in the new space
        info["adopted"] = True
        self.adapt_info = info
        self.dirty = True
        return info

    @staticmethod
    def _norm(y):
        return y / np.maximum(np.linalg.norm(y, axis=1, keepdims=True), 1e-6)

    def drop(self, pid):
        self.vecs.pop(pid, None)
        self.pinned.pop(pid, None)
        self.dirty = True

    # ---------------------------------------------------------- matching
    def _tx(self, x):
        x = np.asarray(x, np.float32)
        if self.proj is None:
            return x
        y = x @ self.proj
        return y / np.maximum(np.linalg.norm(y, axis=-1, keepdims=True), 1e-6)

    def _sim(self, a, b):
        """Similarity on the usual cosine scale, through the learned model if active."""
        if self.proj is None:
            return np.asarray(a, np.float32) @ np.asarray(b, np.float32).T
        a_, c = self.cal
        return np.clip(a_ * (self._tx(a) @ self._tx(b).T) + c, -1.0, 1.0)

    def score(self, vecs, pid, k=3):
        g = self.vecs.get(pid)
        if g is None or len(g) == 0 or vecs is None or len(vecs) == 0:
            return None
        s = self._sim(vecs, g)
        kk = min(k, g.shape[0])
        top = -np.sort(-s, axis=1)[:, :kk]
        return float(top.mean())

    def thresholds(self, kind):
        """(accept, margin). Learned from the gallery when it has enough
        data: accept just above the highest 'different person' similarity."""
        now = time.time()
        if self.th is not None and now - self.th_t < 120:
            return self.th
        self.th_t = now
        base = (0.72, 0.05) if kind == "dinov2" else (0.82, 0.04)
        try:
            # Learn only from named people: an unnamed duplicate of someone is
            # not a "different person" and would push the bar up wrongly.
            people = [p for p, v in self.vecs.items() if len(v) >= 8 and (not self.named or p in self.named)]
            if len(people) < 2:
                self.th = base
                return self.th
            gen, imp = [], []
            for p in people:
                g = self.vecs[p]
                for i in range(len(g)):
                    rest = np.delete(g, i, axis=0)
                    s = self._sim(rest, g[i][None])[:, 0]
                    gen.append(float(np.sort(s)[-3:].mean()))
                    for q in people:
                        if q == p:
                            continue
                        s2 = self._sim(self.vecs[q], g[i][None])[:, 0]
                        imp.append(float(np.sort(s2)[-3:].mean()))
            hi_imp = float(np.percentile(imp, 99)) if imp else base[0]
            lo_gen = float(np.percentile(gen, 10)) if gen else base[0]
            accept = min(max(hi_imp + 0.02, base[0] - 0.06), base[0] + 0.04)
            if lo_gen < accept:          # people overlap: demand a clearer margin
                margin = base[1] + 0.03
            else:
                margin = base[1]
            self.th = (accept, margin)
            print("[REID6] thresholds learned: accept=%.3f margin=%.2f (same-person p10=%.3f, other-person p99=%.3f)"
                  % (accept, margin, lo_gen, hi_imp), flush=True)
        except Exception:
            self.th = base
        return self.th

    def decide(self, vecs, candidates, kind, refs=()):
        """Returns (pid | '?' | None, log text).
        candidates: people this track may be named as.
        refs: people it may NOT be named as here (already in view, or not the
              resident), used only as comparison: if the track looks more like
              one of them, it is not named as a candidate.
        '?' means a known person fits but not clearly enough: keep watching."""
        acc, mar = self.thresholds(kind)
        cs = sorted(((s, p) for p in candidates for s in [self.score(vecs, p)] if s is not None), reverse=True)
        rs = sorted(((s, p) for p in refs if p not in candidates for s in [self.score(vecs, p)] if s is not None),
                    reverse=True)
        allv = sorted(cs + rs, reverse=True)
        self.last_top = cs[:2]
        self.last_best = allv[0][0] if allv else None
        self.last_kind = "none"
        txt = "fp=%s acc=%.2f n=%d" % (",".join("%s:%.3f%s" % (p, s, "" if (s, p) in cs else "(ref)")
                                                for s, p in allv[:3]) or "none",
                                       acc, len(vecs) if vecs is not None else 0)
        if not cs:
            self.last_kind = "nocand"
            return None, txt
        b, pid = cs[0]
        if self.named and pid not in self.named:
            # An unnamed duplicate of a family member (made before they were
            # recognised) must not outvote that family member.
            nm = [(s, p) for s, p in cs if p in self.named]
            if nm and b - nm[0][0] < mar:
                b, pid = nm[0]
        # Competitors: named people and people already in view. Unnamed
        # leftovers do not block naming a family member.
        others = [s for s, p in allv if p != pid and (not self.named or pid not in self.named
                                                      or p in self.named or p in refs)]
        s2 = others[0] if others else None
        self.last_kind = "named"
        if b >= acc and (s2 is None or b - s2 >= mar):
            return pid, "strong | " + txt
        if s2 is not None and b >= LEAD_FLOOR and b - s2 >= LEAD_GAP:
            return pid, "lead | " + txt
        self.last_kind = "looks"
        if rs and ((rs[0][0] >= acc and rs[0][0] - b >= mar)
                   or (rs[0][0] >= LEAD_FLOOR and rs[0][0] - b >= LEAD_GAP)):
            return None, "looks like %s | %s" % (rs[0][1], txt)
        if s2 is not None and b - s2 < 0.02 and b < acc - 0.03:
            # Equally (and only moderately) like everyone: that is what a
            # stranger looks like, not a family member in new clothes.
            self.last_kind = "flat"
            return None, "like nobody in particular | " + txt
        if b >= UNSURE_FLOOR:
            self.last_kind = "unsure"
            return "?", txt
        self.last_kind = "none"
        return None, txt


EMBEDDER = Embedder()
GALLERY = Gallery()
if os.environ.get("VIGIL_REID_NOLOAD") != "1":
    threading.Thread(target=EMBEDDER.load, daemon=True, name="reid-load").start()
