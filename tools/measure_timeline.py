"""At what event count does a duration-proportional timeline stop being legible?

The strip lays events out with `flex: <dur_ms>`, so a block's width is its share
of the run's total duration -- not an equal slice. The question is therefore not
"how many blocks fit" but "at what count does a *typical* block fall under the
smallest rectangle a person can see and click".

Floor: 3 CSS px of block plus a 2px gap. Below 3px a block is a line, not a
rectangle -- it cannot show a colour reliably or be a mouse target.
"""
import random

WIDTH_PX = 1200.0   # a full-width strip on a 13" laptop, the smallest real case
GAP_PX = 2.0
FLOOR_PX = 3.0

# A realistic agent mix, measured against the shapes reeltime actually records.
# Ambient reads are excluded: they are near-zero by nature and the design puts
# them on their own tick sub-track for exactly that reason.
MIX = [("llm", 0.30, 400, 1400), ("http", 0.25, 40, 250),
       ("tool", 0.25, 1, 40), ("chain", 0.20, 1, 30)]


def synth(n, seed=7):
    random.seed(seed)
    out = []
    for _ in range(n):
        r, acc = random.random(), 0.0
        for kind, share, lo, hi in MIX:
            acc += share
            if r <= acc:
                out.append(random.uniform(lo, hi))
                break
    return out


def illegible_fraction(durations):
    total = sum(durations)
    usable = WIDTH_PX - GAP_PX * len(durations)
    if usable <= 0:
        return 1.0
    widths = [usable * (d / total) for d in durations]
    return sum(1 for w in widths if w < FLOOR_PX) / len(widths)


print("width={:.0f}px  gap={:.0f}px  floor={:.0f}px\n".format(WIDTH_PX, GAP_PX, FLOOR_PX))
print("  events   illegible   median px   verdict")
prev_ok = None
for n in (50, 100, 150, 200, 250, 300, 400, 500, 750, 1000, 2000):
    d = synth(n)
    frac = illegible_fraction(d)
    total, usable = sum(d), max(WIDTH_PX - GAP_PX * n, 0)
    widths = sorted(usable * (x / total) for x in d)
    med = widths[len(widths) // 2]
    verdict = "ok" if frac <= 0.10 else ("degrading" if frac <= 0.35 else "unusable")
    print("  {:6d}   {:8.1%}   {:9.2f}   {}".format(n, frac, med, verdict))

# Find the exact crossing where >10% of blocks fall under the floor.
lo, hi = 10, 2000
while lo < hi:
    mid = (lo + hi) // 2
    if illegible_fraction(synth(mid)) > 0.10:
        hi = mid
    else:
        lo = mid + 1
print("\nfirst count with >10% of blocks under {:.0f}px: {}".format(FLOOR_PX, lo))

# Stability across seeds, so the threshold is not one lucky trace.
crossings = []
for seed in range(1, 11):
    a, b = 10, 2000
    while a < b:
        m = (a + b) // 2
        if illegible_fraction(synth(m, seed)) > 0.10:
            b = m
        else:
            a = m + 1
    crossings.append(a)
print("across 10 seeds: min={} max={} median={}".format(
    min(crossings), max(crossings), sorted(crossings)[5]))


# -- the fix: a minimum block width, proportional remainder ---------------
print("\n" + "=" * 62)
print("with a {:.0f}px floor per block and proportional remainder".format(FLOOR_PX))
print("  events   saturated?   widest px   ratio kept")


def with_floor(durations):
    n = len(durations)
    usable = WIDTH_PX - GAP_PX * n
    if usable < FLOOR_PX * n:
        return None                      # no room even at the floor
    spare = usable - FLOOR_PX * n
    total = sum(durations)
    return [FLOOR_PX + spare * (d / total) for d in durations]


for n in (50, 100, 150, 200, 239, 240, 241, 300):
    w = with_floor(synth(n))
    if w is None:
        print("  {:6d}   saturated    {:>9}   {:>10}".format(n, "-", "-"))
        continue
    d = synth(n)
    # Does the widest block still read as clearly longer than the narrowest?
    ratio = max(w) / min(w)
    dur_ratio = max(d) / min(d)
    print("  {:6d}   no           {:9.1f}   {:.0f}x of {:.0f}x".format(
        n, max(w), ratio, dur_ratio))

sat = int(WIDTH_PX // (FLOOR_PX + GAP_PX))
print("\nsaturation point = width / (floor + gap) = {:.0f} / {:.0f} = {}".format(
    WIDTH_PX, FLOOR_PX + GAP_PX, sat))
print("beyond that there is no room for another block at any width, so")
print("bucketing is forced rather than chosen.")
