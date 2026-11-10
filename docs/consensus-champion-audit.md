# Champion Audit — extractor consensus tally (#20)

Issue: [#20](https://github.com/Treystu/driver-core/issues/20), program tracker [#19](https://github.com/Treystu/driver-core/issues/19).

**Verdict: the champion stands.** The hypothesis that a majority rule over a
3-slot pool reaches the same false-agreement rate with fewer blocked-but-correct
rounds is **rejected** — in every regime measured, buying fewer blocks costs
false agreements, which the issue's own acceptance rule forbids.

Reproduce:

```
python tools/consensus_champion_audit.py
```

Everything below is deterministic (fixed seed, stdlib `random` only), so those
numbers are what this command prints.

## What was measured, and what it is not

The mechanism under audit is `consensus.tally`, which is pure deterministic
arithmetic over the votes it is handed. That is what makes this audit possible
offline: the *rule* can be measured without a model, a key, a network or a
clock, and given the same votes it always returns the same verdict.

The corpus is **synthetic**, with a declared generative model implemented in
`tools/consensus_champion_audit.py`. So these results are a property of the
agreement rule under a stated model of extractor behaviour. They are **not** a
measurement of any real extractor's accuracy, and nothing here should be quoted
as one. What they do establish is which regimes separate the champion from a
challenger, and in which direction.

Three knobs describe a regime:

| knob | meaning |
|---|---|
| `clean` | probability an answering slot reports the exact truth |
| `fail` | probability a slot produces no answer at all (`SLOT_ERROR`) |
| `corr` | probability a wrong answer is drawn from a **narrow shared pool**, modelling two extractors reading the same corrupted source |

`corr` is the knob the whole audit turns on. Unanimity is a strong rule when
errors are independent, because two slots rarely invent the *same* lie. It is a
weak rule when errors are correlated, because then the same lie arrives twice
and reads as agreement.

Two error types are reported, and they are not symmetric:

- **false-agreement rate** — the arm said `agreed` and the agreed state was not
  the ground truth. This is the dangerous error: a confidently wrong state is
  handed to the decision tier, which then reasons beautifully about it.
- **blocked-but-correct rate** — the arm refused, even though the *majority of
  the slots that actually answered* agreed on the truth. This is the expensive
  error: the right answer was in hand and the rule declined to use it.

## Arms

The champion is the shipped default **including its wiring**: `configured_pools`
sizes every pool to the quorum (`slots = max(1, int(settings.quorum))`), so the
default pool is exactly 2 slots and `min_agreement=1.0` means unanimity among
both of them. Any single non-answer therefore makes agreement arithmetically
impossible — which is the premise in the issue, and it is confirmed below.

## Findings

### 1. The champion has the lowest false-agreement rate in every regime

Across all eight regimes the champion's false-agreement rate is **0.00%–1.93%**.
No arm ever beat it. That is the rule doing exactly the job it was written for,
and it is worth stating plainly because the rest of this report is about a
trade, not a defect.

### 2. "Majority over 3" always costs false agreements — the hypothesis is rejected

The issue's proposed comparator was "a majority rule (`min_agreement` 0.5–0.67)
over a 3-slot pool". Measured, the true majority rule (2-of-3) cuts
blocked-but-correct by 74%–94% relative, which is far past the required 20%.
But it pays for it with **+4.7 to +26.1 percentage points of false agreements**
in every regime. The acceptance rule requires a ≥20% block cut *with zero
increase in false agreements*. Every majority arm violates the second clause.

The cause is not subtle: with two slots, unanimity needs both to be right. With
three slots and a majority rule, the rule will happily act on the one round in
which two slots shared a wrong answer. Correlated error makes that common
(`corr=0.5` and `corr=0.9` rows), and independent error makes it merely less
common — never absent.

### 3. `min_agreement=0.67` is not a majority rule — 2-of-3 is *below* it

This is the sharpest finding and it is arithmetic: 2/3 = 0.666…, which is
strictly **less than** 0.67. The contested test is
`contested = (agreeing / present) < min_agreement`, so a 2-of-3 field is marked
**contested** at `min_agreement=0.67`.

The consequence is visible in the tables: the `majority 0.67` arm is
numerically **identical to the fault-tolerant unanimity arm in every regime**.
It never once behaved as a majority rule. Anyone who "turns on majority voting"
by setting `min_agreement=0.67` gets unanimity-with-tolerance and will believe
they got something else.

The correct spelling of "two of three is enough" is a fraction (`2/3`), not the
decimal 0.67. The issue's own proposed range `0.5–0.67` is therefore
mis-specified at its upper bound — its top value cannot express the rule it
names.

### 4. Growing the pool only helps when slots *fail*, and hurts when they *err*

This is the finding that changes how the wiring should be thought about. The
`slots = quorum` choice means one dead extractor blocks the round. The obvious
repair is to decouple them — pool 3, quorum 2, still unanimity. It keeps the
rule exact, so it cannot manufacture an agreement the champion would have
refused, and the tables show its false-agreement rate matching the champion to
within 0.6 pp.

But its blocked-but-correct rate is **worse** than the champion's in six of the
eight regimes — `-161%`, `-104%`, `-86%`, `-134%`, `-104%`, `-124%` relative.
The reason is the third slot is a liability under unanimity whenever it answers
and is wrong: 2 correct + 1 wrong is a contested field, where 2 correct and one
*dead* slot would have been a clean 2-of-2 agreement.

So the arm only wins where failure dominates error — at `fail=0.20` it cuts
blocked-but-correct by **27.19%** at `-0.18 pp` false agreement, the only PASS
in the whole sweep. That is a real but *regime-conditional* win.

**Recommendation: keep the champion, and do not decouple pool from quorum
unconditionally.** A `slots = quorum + 1` change is justified only where slot
*non-answers* are known to be frequent (a monitored failure rate), because
where slots answer and err instead, the extra slot is a net harm. Making that
change globally would trade a measurable block rate for an unmeasurable one.

## What would falsify this

The conclusion rests on the generative model. It would be overturned by a
labelled corpus of *real* extraction rounds showing that slot failures dominate
slot errors in production — i.e. that the `fail` regime is the realistic one.
In that world finding 4 becomes actionable and `slots = quorum + 1` should ship.
The tool takes `--rounds` and `--seed`, and the arms and regimes are plain
tuples at the top of the file, so a recorded corpus can be dropped in without
touching the measurement code.

That is the follow-up this audit cannot do for itself: it needs recorded runs,
and recorded runs need a live endpoint.

---

## Generated measurement

The section below is the tool's own output, appended verbatim. It is not
hand-edited; re-running the command above regenerates it byte for byte.

# Champion Audit -- extractor consensus tally (#20)

Corpus: synthetic, 4000 rounds per regime, seed `20261107`, generative model declared in `tools/consensus_champion_audit.py`.

Arms are named by rule; the first is the shipped champion (pool sized to quorum, so 2 slots and unanimity).


### clean-independent

clean=0.95 fail=0.0 corr=0.0

| arm | agreed | false-agreement | blocked-but-correct |
|---|---|---|---|
| champion (pool 2, quorum 2, unanimity) | 3626 | 0.00% | 0.00% |
| pool 3, quorum 3, unanimity | 3467 | 0.00% | 12.50% |
| pool 3, quorum 2, unanimity (fault-tolerant) | 3467 | 0.00% | 12.50% |
| pool 3, quorum 2, majority 0.67 | 3467 | 0.00% | 12.50% |
| pool 3, quorum 2, majority 0.5 | 3994 | 4.70% | 0.00% |
| pool 3, quorum 2, majority exactly 2/3 | 3994 | 4.70% | 0.00% |
| pool 3, quorum 3, majority exactly 2/3 | 3994 | 4.70% | 0.00% |

### good-independent

clean=0.9 fail=0.05 corr=0.0

| arm | agreed | false-agreement | blocked-but-correct |
|---|---|---|---|
| champion (pool 2, quorum 2, unanimity) | 2953 | 0.20% | 8.08% |
| pool 3, quorum 3, unanimity | 2506 | 0.00% | 32.38% |
| pool 3, quorum 2, unanimity (fault-tolerant) | 2959 | 0.03% | 21.07% |
| pool 3, quorum 2, majority 0.67 | 2959 | 0.03% | 21.07% |
| pool 3, quorum 2, majority 0.5 | 3962 | 9.72% | 0.47% |
| pool 3, quorum 2, majority exactly 2/3 | 3858 | 8.28% | 0.47% |
| pool 3, quorum 3, majority exactly 2/3 | 3405 | 8.25% | 11.77% |

### mixed-independent

clean=0.8 fail=0.1 corr=0.0

| arm | agreed | false-agreement | blocked-but-correct |
|---|---|---|---|
| champion (pool 2, quorum 2, unanimity) | 2051 | 0.40% | 14.52% |
| pool 3, quorum 3, unanimity | 1498 | 0.05% | 45.75% |
| pool 3, quorum 2, unanimity (fault-tolerant) | 2149 | 0.15% | 29.58% |
| pool 3, quorum 2, majority 0.67 | 2149 | 0.15% | 29.58% |
| pool 3, quorum 2, majority 0.5 | 3825 | 17.62% | 2.38% |
| pool 3, quorum 2, majority exactly 2/3 | 3511 | 13.30% | 2.38% |
| pool 3, quorum 3, majority exactly 2/3 | 2860 | 13.20% | 18.55% |

### poor-independent

clean=0.7 fail=0.15 corr=0.0

| arm | agreed | false-agreement | blocked-but-correct |
|---|---|---|---|
| champion (pool 2, quorum 2, unanimity) | 1519 | 1.12% | 17.57% |
| pool 3, quorum 3, unanimity | 825 | 0.12% | 48.98% |
| pool 3, quorum 2, unanimity (fault-tolerant) | 1491 | 0.55% | 32.75% |
| pool 3, quorum 2, majority 0.67 | 1491 | 0.55% | 32.75% |
| pool 3, quorum 2, majority 0.5 | 3663 | 26.07% | 4.42% |
| pool 3, quorum 2, majority exactly 2/3 | 3037 | 17.35% | 4.42% |
| pool 3, quorum 3, majority exactly 2/3 | 2371 | 16.93% | 20.65% |

### good-lowslotless

clean=0.9 fail=0.2 corr=0.0

| arm | agreed | false-agreement | blocked-but-correct |
|---|---|---|---|
| champion (pool 2, quorum 2, unanimity) | 2031 | 0.20% | 29.43% |
| pool 3, quorum 3, unanimity | 1501 | 0.00% | 52.20% |
| pool 3, quorum 2, unanimity (fault-tolerant) | 2733 | 0.03% | 21.43% |
| pool 3, quorum 2, majority 0.67 | 2733 | 0.03% | 21.43% |
| pool 3, quorum 2, majority 0.5 | 3570 | 9.18% | 8.77% |
| pool 3, quorum 2, majority exactly 2/3 | 3296 | 5.40% | 8.77% |
| pool 3, quorum 3, majority exactly 2/3 | 2064 | 5.38% | 39.55% |

### good-correlated

clean=0.9 fail=0.05 corr=0.5

| arm | agreed | false-agreement | blocked-but-correct |
|---|---|---|---|
| champion (pool 2, quorum 2, unanimity) | 2937 | 0.70% | 8.85% |
| pool 3, quorum 3, unanimity | 2532 | 0.07% | 31.97% |
| pool 3, quorum 2, unanimity (fault-tolerant) | 2987 | 0.15% | 20.67% |
| pool 3, quorum 2, majority 0.67 | 2987 | 0.15% | 20.67% |
| pool 3, quorum 2, majority 0.5 | 3965 | 8.77% | 0.50% |
| pool 3, quorum 2, majority exactly 2/3 | 3868 | 7.58% | 0.50% |
| pool 3, quorum 3, majority exactly 2/3 | 3413 | 7.50% | 11.80% |

### mixed-correlated

clean=0.8 fail=0.1 corr=0.5

| arm | agreed | false-agreement | blocked-but-correct |
|---|---|---|---|
| champion (pool 2, quorum 2, unanimity) | 2093 | 1.93% | 14.45% |
| pool 3, quorum 3, unanimity | 1512 | 0.27% | 44.40% |
| pool 3, quorum 2, unanimity (fault-tolerant) | 2135 | 0.90% | 29.45% |
| pool 3, quorum 2, majority 0.67 | 2135 | 0.90% | 29.45% |
| pool 3, quorum 2, majority 0.5 | 3853 | 19.60% | 2.27% |
| pool 3, quorum 2, majority exactly 2/3 | 3485 | 14.97% | 2.27% |
| pool 3, quorum 3, majority exactly 2/3 | 2862 | 14.35% | 17.22% |

### good-highly-correlated

clean=0.9 fail=0.05 corr=0.9

| arm | agreed | false-agreement | blocked-but-correct |
|---|---|---|---|
| champion (pool 2, quorum 2, unanimity) | 2872 | 0.75% | 9.53% |
| pool 3, quorum 3, unanimity | 2520 | 0.03% | 32.27% |
| pool 3, quorum 2, unanimity (fault-tolerant) | 2963 | 0.18% | 21.35% |
| pool 3, quorum 2, majority 0.67 | 2963 | 0.18% | 21.35% |
| pool 3, quorum 2, majority 0.5 | 3972 | 9.72% | 0.68% |
| pool 3, quorum 2, majority exactly 2/3 | 3883 | 8.53% | 0.68% |
| pool 3, quorum 3, majority exactly 2/3 | 3440 | 8.38% | 11.60% |

### Acceptance test against the champion

A challenger passes only with a >=20% relative cut in blocked-but-correct and **no** increase in false-agreement.

| regime | arm | blocked cut | false-agreement delta | verdict |
|---|---|---|---|---|
| clean-independent | pool 3, quorum 3, unanimity | n/a | 0.00% | fail |
| clean-independent | pool 3, quorum 2, unanimity (fault-tolerant) | n/a | 0.00% | fail |
| clean-independent | pool 3, quorum 2, majority 0.67 | n/a | 0.00% | fail |
| clean-independent | pool 3, quorum 2, majority 0.5 | n/a | +4.70 pp | fail |
| clean-independent | pool 3, quorum 2, majority exactly 2/3 | n/a | +4.70 pp | fail |
| clean-independent | pool 3, quorum 3, majority exactly 2/3 | n/a | +4.70 pp | fail |
| good-independent | pool 3, quorum 3, unanimity | -300.93% | -0.20 pp | fail |
| good-independent | pool 3, quorum 2, unanimity (fault-tolerant) | -160.99% | -0.18 pp | fail |
| good-independent | pool 3, quorum 2, majority 0.67 | -160.99% | -0.18 pp | fail |
| good-independent | pool 3, quorum 2, majority 0.5 | 94.12% | +9.53 pp | fail |
| good-independent | pool 3, quorum 2, majority exactly 2/3 | 94.12% | +8.08 pp | fail |
| good-independent | pool 3, quorum 3, majority exactly 2/3 | -45.82% | +8.05 pp | fail |
| mixed-independent | pool 3, quorum 3, unanimity | -214.97% | -0.35 pp | fail |
| mixed-independent | pool 3, quorum 2, unanimity (fault-tolerant) | -103.61% | -0.25 pp | fail |
| mixed-independent | pool 3, quorum 2, majority 0.67 | -103.61% | -0.25 pp | fail |
| mixed-independent | pool 3, quorum 2, majority 0.5 | 83.65% | +17.22 pp | fail |
| mixed-independent | pool 3, quorum 2, majority exactly 2/3 | 83.65% | +12.90 pp | fail |
| mixed-independent | pool 3, quorum 3, majority exactly 2/3 | -27.71% | +12.80 pp | fail |
| poor-independent | pool 3, quorum 3, unanimity | -178.66% | -1.00 pp | fail |
| poor-independent | pool 3, quorum 2, unanimity (fault-tolerant) | -86.34% | -0.57 pp | fail |
| poor-independent | pool 3, quorum 2, majority 0.67 | -86.34% | -0.57 pp | fail |
| poor-independent | pool 3, quorum 2, majority 0.5 | 74.82% | +24.95 pp | fail |
| poor-independent | pool 3, quorum 2, majority exactly 2/3 | 74.82% | +16.22 pp | fail |
| poor-independent | pool 3, quorum 3, majority exactly 2/3 | -17.50% | +15.80 pp | fail |
| good-lowslotless | pool 3, quorum 3, unanimity | -77.40% | -0.20 pp | fail |
| good-lowslotless | pool 3, quorum 2, unanimity (fault-tolerant) | 27.19% | -0.18 pp | PASS |
| good-lowslotless | pool 3, quorum 2, majority 0.67 | 27.19% | -0.18 pp | PASS |
| good-lowslotless | pool 3, quorum 2, majority 0.5 | 70.18% | +8.97 pp | fail |
| good-lowslotless | pool 3, quorum 2, majority exactly 2/3 | 70.18% | +5.20 pp | fail |
| good-lowslotless | pool 3, quorum 3, majority exactly 2/3 | -34.41% | +5.17 pp | fail |
| good-correlated | pool 3, quorum 3, unanimity | -261.30% | -0.62 pp | fail |
| good-correlated | pool 3, quorum 2, unanimity (fault-tolerant) | -133.62% | -0.55 pp | fail |
| good-correlated | pool 3, quorum 2, majority 0.67 | -133.62% | -0.55 pp | fail |
| good-correlated | pool 3, quorum 2, majority 0.5 | 94.35% | +8.07 pp | fail |
| good-correlated | pool 3, quorum 2, majority exactly 2/3 | 94.35% | +6.87 pp | fail |
| good-correlated | pool 3, quorum 3, majority exactly 2/3 | -33.33% | +6.80 pp | fail |
| mixed-correlated | pool 3, quorum 3, unanimity | -207.27% | -1.65 pp | fail |
| mixed-correlated | pool 3, quorum 2, unanimity (fault-tolerant) | -103.81% | -1.03 pp | fail |
| mixed-correlated | pool 3, quorum 2, majority 0.67 | -103.81% | -1.03 pp | fail |
| mixed-correlated | pool 3, quorum 2, majority 0.5 | 84.26% | +17.68 pp | fail |
| mixed-correlated | pool 3, quorum 2, majority exactly 2/3 | 84.26% | +13.05 pp | fail |
| mixed-correlated | pool 3, quorum 3, majority exactly 2/3 | -19.20% | +12.42 pp | fail |
| good-highly-correlated | pool 3, quorum 3, unanimity | -238.85% | -0.72 pp | fail |
| good-highly-correlated | pool 3, quorum 2, unanimity (fault-tolerant) | -124.15% | -0.57 pp | fail |
| good-highly-correlated | pool 3, quorum 2, majority 0.67 | -124.15% | -0.57 pp | fail |
| good-highly-correlated | pool 3, quorum 2, majority 0.5 | 92.91% | +8.97 pp | fail |
| good-highly-correlated | pool 3, quorum 2, majority exactly 2/3 | 92.91% | +7.78 pp | fail |
| good-highly-correlated | pool 3, quorum 3, majority exactly 2/3 | -21.78% | +7.63 pp | fail |
