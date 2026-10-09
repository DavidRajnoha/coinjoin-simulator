# EmuCoinJoin

A container-based setup for the emulation of CoinJoin transactions on RegTest network.

## Usage

1. Install [Docker](https://docker.com/), [Python](http://python.org/), and [uv](https://docs.astral.sh/uv/).
2. Clone the repository `git clone --recurse-submodules https://github.com/crocs-muni/coinjoin-emulator`.
3. Install dependencies: `uv sync`.
4. Run the default scenario with the default driver: `uv run python manager.py run`.
   - [Scenario](#scenarios) definition file can be specified using the `--scenario` option.

For more complex setups see section [Advanced usage](#advanced-usage).

## Scenarios

Scenario definition files can be passed to the simulation script using the `--scenario` option. The scenario definition is a JSON file with the following structure:

```json
{
    "name": "default",
    "rounds": 0,
    "blocks": 120,
    "default_version": "2.0.4",
    "distributor_version": "2.0.4",
    "default_anon_score_target": 5,
    "default_redcoin_isolation": false,
    "backend": {
        "MaxInputCountByRound": 200,
        "MinInputCountByRoundMultiplier": 0.2,
        ...
    },
    "wallets": [
        {"funds": [200000, 50000]},
        {"funds": [3000000], "delay_rounds": 5},
        {"funds": [1000000, 50000], "delay_rounds": 3},
        {"funds": [100000, {"value": 200000, "delay_rounds": 5}]},
        {"funds": [200000], "version": "2.0.3"},
        {"funds": [4000000], "wasabi": {"anon_score_target": "25"}},
        {"funds": [3500000], "wasabi": {"redcoin_isolation": true}},
        ...
    ],
}
```

The fields are as follows:
- `name` field is the name of the scenario used for output logs.
- `rounds` field is the number of coinjoin rounds after which the simulation terminates. If set to 0, the simulation will run indefinitely.
- `blocks` field is the number of mined blocks after which the simulation terminates. If set to 0, the simulation will run indefinitely.
- `default_version` field is the string representing of the version of wallet wasabi used for clients without the version specification.
- `distributor_version` field is the string representing of the version of wallet wasabi used for the distributor client.
- `default_anon_score_target` field sets the default value of target anon score.
- `default_redcoin_isolation` field sets the default option for redcoin isolation.
- `backend` field is the configuration for the `wasabi-backend` container used in the simulation. The provided fields update the defaults.
- `wallets` field is a list of wallet configurations. Each wallet configuration is a dictionary with the following fields:
  - `funds` is a list of funds (`int`s or `dict`s) the wallet will use for coinjoins. In case of a dictionary, the following keys are supported:
    - `value` is the amount of funds the wallet will use for coinjoins.
    - `delay_blocks` is the number of blocks the distributor will wait before sending the corresponding funds to the wallet.
    - `delay_rounds` is the number of coinjoin rounds the distributor will wait before sending the corresponding funds to the wallet.
  - `delay_blocks` is the number of blocks the wallet will wait before participating.
  - `delay_rounds` is the number of coinjoin rounds the wallet will wait before participating.
  - `stop_blocks` is the number of blocks after which the wallet will stop participating.
  - `stop_rounds` is the number of rounds after which the wallet will stop participating.
  - `version` is the string representation of wallet wasabi version used for client running this wallet.
  - `wasabi` is an optional object with Wasabi-specific wallet settings:
    - `anon_score_target` is the target anon score of the wallet.
    - `redcoin_isolation` is a boolean value indicating whether the wallet should use redcoin isolation.
    - `skip_rounds` is a list of round numbers the wallet skips.

## Engine
You can run the simulation with different CoinJoin protocols. Currently, Wasabi and Joinmarket are supported. 
The default protocol is Wasabi. To run the simulation with Joinmarket, use the `--engine joinmarket` option.  


### JoinMarket round-event statuses

`data/joinmarket_round_events.json` records each round's `status`:

- `started`: the attempt is waiting for a mined destination match.
- `failed`: the attempt failed.
- `confirmed`: the destination belongs to one round and matches one mined transaction.
- `multiple_matches`: the destination matches several transactions.
- `duplicate_destination`: several rounds share the same destination address.

`duplicate_destination` takes precedence over the match count. Matching transactions
remain in `destination_matches` for diagnostics, but cannot confirm such a round.
Both conflict statuses make the producer-label manifest incomplete.

`execution_status` preserves what the emulator itself knows about the attempt,
while `status` remains the chain-reconciliation result. The record is written
before the start RPC, so a request that jmwalletd executed but never answered
still has its destination on record:

- `requested`: the start RPC was sent and the run ended before it was answered.
- `started`: jmwalletd acknowledged the start.
- `unknown`: the start RPC failed without saying whether jmwalletd executed it
  (timeout, lost connection); the chain reconciliation decides the outcome.
- `failed`: an acknowledged attempt timed out locally.

## Advanced usage

The simulation script enables advanced configuration for running on different container platforms with various networking setups. This section describes the advanced configuration and shows common examples.

### Raw Bitcoin node data

`run --download-btc-data <dir>` copies the node's datadir (`--download-path`,
default `btc-node:/home/bitcoin/data/`) out of the container after the logs are
stored and before the resources are removed. The copy is a consistent snapshot:
the node is asked to flush its block index and chainstate first, every process
in its container is frozen while the archive is read, and the copy is repeated
when a block arrived in between. The archive is streamed to disk, so the datadir
may be larger than the available memory. The recorded height is the connected
tip; a block accepted at that very instant is connected by whoever opens the copy.

### Backend driver


#### Docker

The default driver is `docker`. Running `docker` requires [Docker](https://www.docker.com/) installed locally and running.

JoinMarket tumbler scenarios using a round limit must also set `blocks` to a
positive limit, which bounds the run.

#### Podman

*Podman support will be likely **removed** in the future versions.*

To run the simulation using `podman`, specify it as driver using `--driver podman` option.

The driver requires [Podman](https://podman.io/) being installed and you may also need to override default IP addresses to communicate via localhost using `--control-ip` and `--wasabi-backend-ip` options. 


#### Kubernetes

To run the simulation on a [Kubernetes](https://kubernetes.io/) cluster, use the `kubernetes` driver. The driver requires a running Kubernetes cluster and `kubectl` configured to access the cluster. 

When the manager itself runs inside the cluster, explicitly pass `--in-cluster`
before `run` (for example, `python manager.py --driver kubernetes --in-cluster run ...`).
This selects service-account credentials and Service DNS endpoints. The manager
does not enable this mode from `KUBERNETES_SERVICE_HOST`. A manager running outside
the cluster with a kubeconfig should omit the flag.

The `kubernetes` driver relies on used images being accessible publicly from [DockerHub](https://hub.docker.com/). For that, build the images in `containers` directory manually and upload them to the registry. Afterwards, specify the image prefix using `--image-prefix` option when starting the simulation.

In case *NodePorts* are not supported by your cluster, you may also need to run a proxy to access the services, e.g., [Shadowsocks](https://shadowsocks.org/). Use the `--proxy` option to specify the address of the proxy.

If you need to specify custom namespace, use the `--namespace` option. If you also need to reuse existing namespace, use the `--reuse-namespace` option.

##### Example

Running the simulation on a remote cluster using pre-existing namespace and a proxy reachable on localhost port 8123:
```bash
python manager.py run --driver kubernetes --namespace custom-coinjoin-ns --reuse-namespace --image-prefix "crocsmuni/" --proxy "socks5://127.0.0.1:8123" --scenario "scenarios/uniform-dynamic-500-30utxo.json"
```

#### Running from an in-cluster orchestrator

The command above drives the simulation from your machine, so it has to stay
connected for the whole run. For long runs, deploy the orchestrator into the
cluster and control it with `manager/remote_cli.py`. The orchestrator runs
`manager.py` inside the cluster, so a dropped connection does not end the run.

```bash
# once per namespace: applies containers/emulator-manager/*.yaml
python manager/remote_cli.py --namespace NS deploy --image-prefix "registry.example/"

# copy scenarios into the orchestrator's workspace
kubectl cp scenarios/my_batch NS/$(kubectl get pod -n NS -l app=emulation-manager -o name | cut -d/ -f2):/workspace/scenarios/my_batch

# one simulation, or every *.json in a directory in sorted order with cleanup in between
python manager/remote_cli.py --namespace NS run --scenario /workspace/scenarios/my_batch/one.json
python manager/remote_cli.py --namespace NS run --scenario-dir /workspace/scenarios/my_batch
```

Paths passed to `run` are paths *inside* the orchestrator container, not on your machine.
Once a run is started, the other commands find it from `.run-<namespace>`; you do not
need to say whether it was a single run or a batch:

| Command | Effect |
| --- | --- |
| `status` | progress of the active run (batch: current scenario, how many done) |
| `logs -f` | stream the run's output |
| `skip` | batch only: abandon the current scenario, continue with the next |
| `stop` | stop the run (batch: the whole batch) |
| `download-logs -n 3` | fetch the last 3 finished run archives |
