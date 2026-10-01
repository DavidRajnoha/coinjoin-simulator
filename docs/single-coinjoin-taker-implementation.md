# Single-Coinjoin Taker Implementation

## Overview

This document describes the changes needed to implement takers that participate in a limited number of coinjoins (e.g., just one) and then terminate, rather than continuously participating throughout the simulation.

## Current Behavior

Both `TakerClient` and `TumblerTakerClient` are designed for **continuous operation**:
- They repeatedly initiate coinjoins throughout the simulation
- After completing a coinjoin, they wait `time_between_rounds` blocks
- They automatically start another coinjoin
- This cycle continues until the simulation ends

### Current Taker Lifecycle

1. Start coinjoin when not paused and no coinjoin in progress
2. Run coinjoin for 8 blocks (hardcoded)
3. Stop coinjoin
4. Wait `time_between_rounds` blocks
5. Go to step 1

**Files involved:**
- `manager/wasabi_clients/joinmarket_clients/joinmarket_clients.py:76-144` (TakerClient)
- `manager/wasabi_clients/joinmarket_clients/joinmarket_clients.py:332-413` (TumblerTakerClient)

## Existing Mechanisms

### Unused `stop` Parameter

The `JoinMarketClientServer` base class has a `stop` parameter that is defined but never used:

```python
# manager/wasabi_clients/joinmarket_clients/joinmarket_client_base.py:38
def __init__(self, ..., stop=(0, 0), ...):
    self.stop = stop  # Line 56
```

It's parsed from wallet config (line 125) but no code references it to terminate clients.

### No Completion Concept

- No `is_completed()` or `is_done()` method in any client class
- No mechanism to mark a client as "finished"
- The `self.clients` list in the engine is never modified after initialization
- No filtering of completed clients in update loops

## Proposed Implementation

### 1. Configuration Layer

**File:** `manager/commands/genscen_joinmarket.py`

Add a new parameter to wallet configurations:

```json
{
  "type": "taker",
  "target_coinjoin_count": 1,
  "funds": [...],
  "delay_blocks": 0,
  "time_between_rounds": 0
}
```

**Parameters:**
- `target_coinjoin_count`: Number of coinjoins before termination
  - `0` = unlimited (default, maintains current behavior)
  - `1` = single coinjoin
  - `N` = N coinjoins

**Example scenario generation:**

```bash
python manager.py genscen-joinmarket \
  --name "single_coinjoin_test" \
  --maker-count 80 \
  --taker-count 4 \
  --taker-target-coinjoins 1 \  # New argument
  --tumbler-taker-count 0 \
  --block-count 200
```

### 2. Client State Tracking

**File:** `manager/wasabi_clients/joinmarket_clients/joinmarket_clients.py`

#### TakerClient Changes (lines 76-144)

Add state tracking in `__init__`:

```python
def __init__(self, **kwargs):
    super().__init__(**kwargs)
    self.target_coinjoin_count = kwargs.get("target_coinjoin_count", 0)
    self.coinjoin_count = 0
    self.is_completed = False
```

Modify the `update()` method:

```python
def update(self, current_block, current_round):
    """Start/stop coinjoins and track completion"""

    # If already completed, return 0 (no-op)
    if self.is_completed:
        return 0

    self.update_status()
    delta = 0

    if not self.coinjoin_in_process and not self.is_paused(current_block):
        # Start new coinjoin
        offer = self.get_offer(current_round)
        offer["destination"] = self.get_new_address()
        self.start_coinjoin(**offer)
        self.coinjoin_start = current_block
        self.coinjoin_in_process = True
        delta = +1

    # Check if coinjoin timed out (8 blocks)
    elif self.coinjoin_in_process and self.coinjoin_start + 8 < current_block:
        self.stop_coinjoin()
        self.coinjoin_in_process = False
        self.coinjoin_count += 1  # INCREMENT COUNTER

        # Check if we've reached target
        if self.target_coinjoin_count > 0 and self.coinjoin_count >= self.target_coinjoin_count:
            self.is_completed = True
            print(f"Client {self.name} completed {self.coinjoin_count} coinjoins")
            return -1

        # Continue with normal behavior for unlimited coinjoins
        self.next_coinjoin_allowed = current_block + self.time_between_rounds
        delta = -1

    return delta
```

#### TumblerTakerClient Changes (lines 332-413)

Similar modifications needed:

```python
def __init__(self, **kwargs):
    super().__init__(**kwargs)
    self.target_coinjoin_count = kwargs.get("target_coinjoin_count", 0)
    self.completed_coinjoins = 0
    self.is_completed = False
    # ... existing code ...

def update(self, current_block, current_round):
    """Start tumbler schedule and track completion"""

    if self.is_completed:
        return 0

    # ... existing logic ...

    # After detecting coinjoin completion (when delta = +1):
    self.completed_coinjoins += delta
    if self.target_coinjoin_count > 0 and self.completed_coinjoins >= self.target_coinjoin_count:
        self.is_completed = True
        print(f"Tumbler {self.name} completed {self.completed_coinjoins} coinjoins")

    return delta
```

### 3. Base Client Class

**File:** `manager/wasabi_clients/joinmarket_clients/joinmarket_client_base.py`

Add completion tracking to base class (lines 27-67):

```python
def __init__(self, ..., target_coinjoin_count=0, ...):
    # ... existing code ...
    self.target_coinjoin_count = target_coinjoin_count
    self.is_completed = False
    self.completion_timestamp = None

def mark_completed(self):
    """Mark client as completed with timestamp"""
    from time import time
    self.is_completed = True
    self.completion_timestamp = time()
```

Update `from_wallet()` method (lines 96-142):

```python
@classmethod
def from_wallet(cls, name: str, port: int, wallet: dict, host: str, proxy=""):
    type_ = wallet.get("type", "maker")
    tumbler_options = wallet.get("tumbler_options", {})
    target_coinjoin_count = wallet.get("target_coinjoin_count", 0)

    # ... client class selection logic ...

    client = client_cls(
        name=name,
        port=port,
        type=type_,
        delay=(wallet.get("delay_blocks", 0), wallet.get("delay_rounds", 0)),
        stop=(wallet.get("stop_blocks", 0), wallet.get("stop_rounds", 0)),
        offers=wallet.get("offers", []),
        tumbler_options=tumbler_options,
        time_between_rounds=wallet.get("time_between_rounds", 0),
        target_coinjoin_count=target_coinjoin_count,  # NEW
        has_fidelity_bonds=has_fidelity_bonds,
        host=host,
        proxy=proxy
    )

    # ... existing code ...
    return client
```

### 4. Engine Update Loop

**File:** `manager/engine/joinmarket_engine.py`

Two implementation approaches:

#### Option A: Skip Completed Clients (Recommended)

Minimal changes, safer approach:

```python
# Lines 308-320 - Synchronous version
def update_coinjoins_joinmarket(self):
    for client in self.clients:
        # Skip completed clients
        if hasattr(client, 'is_completed') and client.is_completed:
            continue

        try:
            delta = client.update(self.current_block, self.current_round)
            self.current_round += delta
        except Exception as e:
            print(f"- could not update {client.name} ({e})")
```

```python
# Lines 322-368 - Async version
async def update_coinjoins_joinmarket_async(self):
    """Async version: Update all clients in parallel using asyncio.gather()"""
    client_tasks = []

    for client in self.clients:
        # Skip completed clients
        if hasattr(client, 'is_completed') and client.is_completed:
            continue

        task = self._update_client_async(client)
        client_tasks.append(task)
        jitter = random.uniform(0.01, 0.05)
        await asyncio.sleep(jitter)

    # ... rest of existing code ...
```

#### Option B: Remove Completed Clients

More invasive but cleaner:

```python
def update_coinjoins_joinmarket(self):
    # Remove completed clients from list
    initial_count = len(self.clients)
    self.clients = [c for c in self.clients
                    if not (hasattr(c, 'is_completed') and c.is_completed)]

    if len(self.clients) < initial_count:
        print(f"Removed {initial_count - len(self.clients)} completed clients")

    # Update remaining clients
    for client in self.clients:
        try:
            delta = client.update(self.current_block, self.current_round)
            self.current_round += delta
        except Exception as e:
            print(f"- could not update {client.name} ({e})")
```

### 5. Scenario Generation Updates

**File:** `manager/commands/genscen_joinmarket.py`

Add command-line argument (around line 418):

```python
parser.add_argument("--taker-target-coinjoins", type=int, default=0,
                    help="number of coinjoins per taker (0 = unlimited)")
parser.add_argument("--tumbler-taker-target-coinjoins", type=int, default=0,
                    help="number of coinjoins per tumbler taker (0 = unlimited)")
```

Update taker wallet creation (around lines 575-594):

```python
# TAKERS
taker_delays = parse_delays(args.taker_delays, args.taker_count)
for idx in range(args.taker_count):
    n_utxos = random.randint(taker_min_utxos, taker_max_utxos)

    # ... existing UTXO generation ...

    wallet = {
        "funds": funds,
        "type": "taker"
    }

    delay = taker_delays[idx] if idx < len(taker_delays) else 0
    if delay:
        wallet["delay_blocks"] = delay

    # Add target_coinjoin_count if specified
    if args.taker_target_coinjoins > 0:
        wallet["target_coinjoin_count"] = args.taker_target_coinjoins

    scenario["wallets"].append(wallet)
```

Similar updates for tumbler takers (around lines 596-615).

## Summary of Changes

| Level | File | Changes | Lines |
|-------|------|---------|-------|
| **Config** | `genscen_joinmarket.py` | Add `target_coinjoin_count` parameter | 418-420, 575-615 |
| **Client Base** | `joinmarket_client_base.py` | Add `is_completed` flag; accept parameter | 27-67, 96-142 |
| **TakerClient** | `joinmarket_clients.py` | Track `coinjoin_count`; check completion | 76-144 |
| **TumblerTaker** | `joinmarket_clients.py` | Track `completed_coinjoins`; check completion | 332-413 |
| **Engine Sync** | `joinmarket_engine.py` | Skip completed clients | 308-320 |
| **Engine Async** | `joinmarket_engine.py` | Skip completed clients | 322-368 |

## Design Decisions

### 1. Completion Tracking Approach

**Chosen: Skip approach (Option A)**
- Non-invasive
- Maintains client list integrity
- Easier to debug
- Lower risk

### 2. Configuration Style

**Chosen: `target_coinjoin_count` (integer)**
- More flexible than boolean `single_coinjoin`
- Allows 1, 2, 3, ..., N coinjoins
- Default 0 = unlimited (backward compatible)

### 3. Coinjoin Counting

**Count completed coinjoins only:**
- After 8-block timeout
- Not partial/in-progress coinjoins
- Ensures privacy benefit is achieved

### 4. Backward Compatibility

**Fully backward compatible:**
- Default `target_coinjoin_count = 0` maintains current behavior
- No changes to existing wallet configs required
- Works with both sync and async update loops

## Testing Considerations

### Unit Tests

1. TakerClient with `target_coinjoin_count=1` completes after 1 coinjoin
2. TumblerTakerClient with `target_coinjoin_count=1` completes after 1 coinjoin
3. Client with `target_coinjoin_count=0` continues indefinitely
4. Completed client is skipped in update loop
5. Multiple single-coinjoin takers don't interfere

### Integration Tests

1. Scenario with mixed takers (some single-coinjoin, some continuous)
2. Verify coinjoin mixing works correctly
3. Verify completed clients don't affect round/block counts
4. Test with both sync and async engine modes

### Edge Cases

1. Single-coinjoin taker with `delay_blocks` (starts late but participates once)
2. Single-coinjoin taker that fails first coinjoin (retry logic needed?)
3. Pause state interaction (`is_paused` vs `is_completed` priority)
4. All takers complete before simulation ends

## Example Usage

### Command Line

```bash
# Generate scenario with 4 single-coinjoin takers
python manager.py genscen-joinmarket \
  --name "single_coinjoin_test" \
  --maker-count 80 \
  --taker-count 4 \
  --taker-target-coinjoins 1 \
  --tumbler-taker-count 0 \
  --block-count 200
```

### Manual Scenario File

```json
{
  "name": "single_coinjoin_test",
  "default_version": "joinmarket",
  "rounds": 0,
  "blocks": 200,
  "wallets": [
    {
      "funds": [100000000, 50000000],
      "type": "taker",
      "target_coinjoin_count": 1,
      "delay_blocks": 0
    },
    {
      "funds": [500000000],
      "type": "maker",
      "offers": [{
        "txfee": 0,
        "cjfee_a": 1000,
        "cjfee_r": 0,
        "ordertype": "sw0absoffer",
        "minsize": 100000,
        "maxsize": 450000000
      }]
    }
  ]
}
```

## Future Enhancements

1. **Retry logic**: If a coinjoin fails, should it count toward the limit?
2. **Time-based termination**: Stop after N blocks instead of N coinjoins
3. **Conditional termination**: Stop when wallet reaches certain privacy level
4. **Cleanup**: Optionally shut down completed client containers to save resources

## References

- Original discussion: User inquiry about taker transaction frequency
- Related issue: Unused `stop` parameter in client base class
- Performance note: Completed clients still consume minimal resources in skip approach
