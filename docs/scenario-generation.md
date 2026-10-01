# Experiment scenarios and required data — JoinMarket deanonymization campaign
 
Task doc for work inside the **coinjoin-simulator** repository. Self-contained: no other documents needed.
 
---
 
## 1. What the research is
 
We evaluate a passive on-chain attack on JoinMarket. An observer with nothing but the blockchain and a set of transactions labelled as JoinMarket tries to:
 
1. **Per transaction ("coinjoin sudoku")** — partition the inputs among participants and pair each participant with its change output. JoinMarket's fee asymmetry makes this yield *role labels* too: makers gain a small positive amount, the single taker pays for everything and is the only participant with a negative balance.
2. **Across transactions ("chaining")** — link consecutive transactions of one tumbler run by following the taker's coins, so a remix chain can be traced from its root to its leaf.
 
The claim under test is that repeated mixing does not deliver the anonymity growth users expect, because roles are stable and leak through values and fees.
 
Runs are scored by comparing the blackbox attribution against ground truth from the wallets. Three metrics:
 
- **Sudoku correctness** — share of transactions where exactly the taker's input subset is labelled taker.
- **Taker output recovery** — share of taker coinjoin outputs correctly attributed.
- **Chain tracing** — share of tumbler runs whose leaf transaction receives the same chain identity as its root.
 
The attack's difficulty is driven by scenario parameters: counterparties per coinjoin, inputs per participant, and the spread of maker fees. The campaign sweeps those.
 
---
 
## 2. What to produce
 
Scenario definitions covering the configurations in §4, one per (configuration, seed), each producing the data in §3. A generator is fine if it reduces duplication; what matters is that a configuration is recoverable from the artifact and that the required records come out.
 
---
 
## 3. Data every run must yield
 
This section is the contract. A run that completes but does not yield these records is a wasted 24 hours.
 
**Per transaction**
- txid and block height.
- For each participant: a stable wallet identifier, its input outpoints, its coinjoin output, its change output if any, **its role in *this* transaction**, and its net balance (inputs minus outputs).
- Coinjoin amount and miner fee.
 
Role must be recorded per (wallet, transaction), not per wallet. A wallet's role can change between transactions of the same run — that is precisely what one of the experiments manipulates — so a single role label per wallet silently corrupts the scoring.
 
**Per wallet**
- The identifier used consistently across the whole run.
- Funding UTXOs: how many, what values, which block they arrived in.
- The behaviour it was configured with, including any schedule it was given.
 
**Per tumbler run** (the unit chain tracing is scored on)
- The ordered sequence of that wallet's transactions, from the root (first spend of funding) to the leaf (final payout to an external destination), including any transaction in which the wallet participated in a role other than taker.
- The destination addresses.
 
**Per maker / market**
- Each maker's advertised offer over time: absolute fee, relative fee, order type, minimum and maximum size — and which offer was in force when it was selected.
- The fee each maker actually realized in each transaction it served.
 
**Per run**
- Configuration parameters and the seed.
- Start and end block heights; how many coinjoins completed and how many failed, with the failure reason.
 
The last item is what separates a negative result from a broken run.
 
---
 
## 4. Configuration matrix
 
**Common to every run:** tumbler with 4 mixdepths, 3 destination addresses, 8 tumbler runs per emulation (16 if capacity allows — it doubles the chain-level sample at the same wall-clock), 10 UTXOs per wallet unless the configuration sweeps it. Counts are "existing runs reusable as seed 1" and "new seeds to add", targeting 3 seeds per configuration. One emulation is roughly 24 h.
 
| Priority | Configuration (counterparties, UTXOs) | Existing | Add | Purpose |
|---|---|---|---|---|
| needed | (5,1), 10 | 1 | 2 | weak anchor, Exp. 1 |
| needed | (9,1), 10 | 1 | 2 | JoinMarket default; shared baseline for Exp. 1 and 2 |
| needed | (15,1), 10 | 0 | 3 | our recommended setting |
| needed | (9,1), 3 | 1 | 2 | weak anchor, Exp. 2 |
| needed | (9,1), 25 | 1 | 2 | hard anchor, Exp. 2 |
| needed | (5,1), 3, +50 single takers | 0 | 2 | Exp. 3, weakest setting |
| recommended | (7,1), 10 | 1 | 2 | fills the trend plot |
| recommended | (13,1), 10 | 1 | 2 | fills the trend plot |
| recommended | (9,1), 5 | 1 | 2 | fills the trend plot |
| recommended | (9,1), 10, quantized fees | 0 | 3 | Exp. 4 |
| optional | (11,1), 10 | 1 | 2 | only if the trend is gappy |
| optional | (9,1), 15 | 1 | 2 | only if the trend is gappy |
| optional | (9,1), 3/5/10/15, +50 takers | 4 | 0 | supplementary to Exp. 3 |
| optional | (9,1), 10, role switching on / off | 0 | 6 | Exp. 5, paired arms |
 
---
 
## 5. Per experiment
 
Each entry states what varies, what must be held constant, what extra records are needed beyond §3, and how many observations the claim requires.
 
### Exp. 1 — counterparties
 
*Varies:* counterparties per coinjoin, (5,1) / (9,1) / (15,1), at 10 UTXOs. 9 is the JoinMarket default, 15 is what we recommend to users.
*Constant:* everything else, including the market parametrization.
*Extra records:* the **actual** participant count per transaction, not just the configured range — the range spreads, and the analysis regresses solvability on the realised count.
*Sample:* per-transaction proportions, roughly 100 transactions per emulation, 3 seeds per point.
 
### Exp. 2 — inputs per participant
 
*Varies:* 3 / 10 / 25 UTXOs per wallet at 9 counterparties. The (9,1)/10 cell is shared with Exp. 1 — do not run it twice.
*Extra records:* the actual input count per participant per transaction.
*Sample:* as Exp. 1.
 
### Exp. 3 — orphan single takers
 
*Varies:* 50 takers that perform exactly one coinjoin each, entering on a delay, at the attacker-friendliest setting (5 counterparties, 3 UTXOs).
*Extra records:* which transaction each single taker appeared in, and whether every other participant's coinjoin output in that transaction was later spent — the heuristic under test identifies a taker by eliminating all makers, so the record must show whether the elimination was even possible.
*Sample:* 50 per run gives a clean binomial test against the 1-in-5 chance baseline.
 
### Exp. 4 — quantized fees
 
*Question:* fee quantization was deployed as a privacy measure against a different attack — identifying which maker produced a given output from its exact fee. It does not change the sign of a participant's balance, which is what our role labelling uses, and it *reduces* fee spread, which may make chaining easier. Does it leave our metrics unchanged, or improve them?
 
*Varies:* maker fees are drawn from a small public grid instead of a continuous distribution, and per-announcement fee randomization is off. Grid: relative `2e-5, 5e-5, 1e-4, 2e-4, 5e-4, 1e-3, 2e-3, 5e-3, 1e-2, 2e-2, 5e-2, 1e-1`; absolute `100, 200, 500, 1000, 2000, 5000, 10000` sats.
*Constant:* the (9,1)/10 baseline in every other respect, **including the client implementation**. The comparison is against the baseline run with empirical fees; changing implementation at the same time would confound the result.
 
*Extra records:*
- **Manipulation check:** the count of distinct realized fee values per transaction. If fees did not actually collide, the experiment did not test anything.
- The number of candidate solutions per transaction, split into those that differ only in *which maker is which* and those that differ in *which input subset is the taker's*. Quantization is expected to increase the first and leave the second alone — that split is the result, not the raw solution count.
 
*Sample:* 3 seeds, compared against the 3 baseline seeds.
 
### Exp. 5 — role switching
 
*Question:* a tumbler that acts as a maker between its own taker phases breaks the assumption that a wallet keeps one role, which is what chaining relies on. Does that defeat chain tracing in practice?
 
*Varies:* one knob — role switching on or off.
*Constant:* **everything else, paired.** Same seed, same wallet population, same funding, same market, same client implementation on both arms. A comparison against a differently-configured or differently-implemented run measures the wrong difference.
 
*Extra records:*
- Every interlude in which a tumbler wallet acted as a maker: when it started and ended, **how many coinjoins it actually served**, the txids of those coinjoins, and the UTXOs it contributed and received.
- Whether the coins received in an interlude were later spent by the same wallet as taker inputs, and in which transaction. This is the link the attack would have to follow.
- The interlude participant's net balance in each such transaction. An interlude that charges no coinjoin fee and contributes no transaction fee has a balance of exactly zero, which is a distinguishing feature in its own right; we need it recorded rather than inferred.
 
*Validity:* an interlude only tests anything if some taker selected it. A run whose interludes served zero coinjoins is a **null measurement** and must be reported as one — "role switching did not reach the chain" is a different finding from "role switching did not help", and conflating them would be the easiest way to get this experiment wrong. If uptake is consistently zero, the background taker population is the thing to change, and that needs saying before the seeds are spent.
 
*Sample:* chain tracing is scored per tumbler run, 8 per emulation. Three seeds per arm gives 24 chains per arm, which is the minimum that separates arms — hence 6 runs for the paired design.
 
---
 
## 6. Market parametrization
 
- Maker balances, offer sizes and fees should be sampled from quantiles of real orderbook snapshots (May–November 2025), using **bonded** offers, since takers select bond-weighted makers the large majority of the time. Keep the absolute/relative fee split fixed at 50/50 across makers, except in Exp. 4.
- Size the maker population from worst-case simultaneous demand: concurrent tumbler runs × maximum counterparties, plus headroom for makers that are busy or decline. The (15,1) configuration binds.
- The environment has been run with up to 168 clients; the funding phase is the throughput bottleneck, not the coinjoins.
- Keep the minimum coinjoin amount consistent with maker minimum sizes. If the tumbler splits an amount below what makers will serve, the run stalls and produces no data.
 
---
 
## 7. Things that silently invalidate a run
 
- **Timing coupled to block time.** Transaction timing must leave room for the confirmations that commitment sourcing needs; the repo runs shortened block times for this reason. Compressing timings further caused transaction failures in earlier campaigns.
- **Tumbler restarts.** Derived from the client's maker timeout; too short an inter-transaction interval triggers restarts that pollute the chain structure.
- **Solver input limit.** Transactions above roughly 25 inputs are counted as unsolved. Expected for the 25-UTXO cell, but not elsewhere — if it happens unintentionally, coverage drops without any signal that it did.
- **Stalls for lack of liquidity** produce short or truncated chains that look like attack failures. Distinguish them by the per-run completed/failed counts from §3.
- **Seeds.** Existing runs count as seed 1; new runs are seeds 2 and 3. Results are reported as mean ± 95% CI, so the seed must be recoverable.
- **Fidelity bonds.** In-repo documentation says bonds are unsupported, while the client has a timelock-address call and the analysis side parses bonds out of scenarios. Verify what your branch supports before relying on bonded-maker selection; it affects §6.
 
---
 
## 8. Scenario format
 
`JOINMARKET.md` in this repo is the authority on the file format, wallet configuration, taker and maker offers, the full tumbler option table, and where the logs land. Read it first. The mapping from experiment parameters:
 
| Experiment parameter | Where it lives |
|---|---|
| Counterparties per coinjoin | the tumbler option controlling maker count as `[mean, spread]`; keep the minimum below the mean so runs do not stall |
| UTXOs per participant | **length of the wallet's `funds` list** — each entry becomes one invoice, i.e. one UTXO |
| Delayed entrants (Exp. 3) | `funds` entries as objects with a block or round delay |
| Maker fee policy | the wallet's `offers` entries |
| Tumbler run shape | destination-address count, mixdepth count, transactions per mixdepth |
| Run length | top-level stop conditions in rounds or blocks |
 
A tumbler wallet is a taker wallet carrying a tumbler options object. Existing examples live in `scenarios/joinmarket/`; note that `default.json` there is a symlink to a file absent on some branches.
 
---
 
## 9. Acceptance criteria
 
1. Every "needed" configuration has definitions for three seeds, with the configuration recoverable from the artifact.
2. One short smoke run per distinct shape (plain tumbler, delayed single takers, quantized fees, role switching) completes **and is checked against §3** — the records exist and are populated — before the long campaign is queued.
3. For Exp. 4, the manipulation check shows fees actually collided.
4. For Exp. 5, the two arms are identical apart from the one knob, and interlude uptake is reported per run.
5. A checklist of which definitions exist and which runs remain to be executed.
 
---
 
## 10. Open items
 
1. Which branch is current for JoinMarket work — scenario and tumbler support are not on the same branch as some of the other JoinMarket code.
2. Whether fidelity bonds are usable on that branch (§7).
3. For Exp. 5, which implementation provides role switching, and whether the background taker population will select a zero-fee, unbonded interlude often enough for the arm to carry information.
