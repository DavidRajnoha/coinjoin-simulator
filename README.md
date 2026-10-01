# EmuCoinJoin

A container-based setup for the emulation of CoinJoin transactions on RegTest network.

## Usage

1. Install [Docker](https://docker.com/) and [Python](http://python.org/).
2. Clone the repository `git clone --recurse-submodules https://github.com/crocs-muni/coinjoin-emulator`.
3. Install dependencies: `pip install -r requirements.txt`.
4. Run the default scenario with the default driver: `python manager.py run`.
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
        {"funds": [4000000], "anon_score_target": "25"},
        {"funds": [3500000], "redcoin_isolation": true},
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
  - `anon_score_target` is the target anon score of the wallet.
  - `redcoin_isolation` is a boolean value indicating whether the wallet should use redcoin isolation.

## Engine
You can run the simulation with different CoinJoin protocols. Currently, Wasabi and Joinmarket are supported. 
The default protocol is Wasabi. To run the simulation with Joinmarket, use the `--engine joinmarket` option.  


## Advanced usage

The simulation script enables advanced configuration for running on different container platforms with various networking setups. This section describes the advanced configuration and shows common examples.

### Backend driver


#### Docker

The default driver is `docker`. Running `docker` requires [Docker](https://www.docker.com/) installed locally and running.

#### Podman

*Podman support will be likely **removed** in the future versions.*

To run the simulation using `podman`, specify it as driver using `--driver podman` option.

The driver requires [Podman](https://podman.io/) being installed and you may also need to override default IP addresses to communicate via localhost using `--control-ip` and `--wasabi-backend-ip` options. 


#### Kubernetes

To run the simulation on a [Kubernetes](https://kubernetes.io/) cluster, use the `kubernetes` driver. The driver requires a running Kubernetes cluster and `kubectl` configured to access the cluster. 

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
cluster instead and control it with `manager/remote_cli.py`. The orchestrator
runs `manager.py` for you inside the cluster, so a dropped laptop connection
does not end the simulation.

```bash
# once per namespace
python manager/remote_cli.py --namespace NS deploy --image-prefix "drajnoha/"

# one simulation
python manager/remote_cli.py --namespace NS run --scenario /app/scenarios/joinmarket/test/ng_basic.json

# or a batch: every *.json in the directory, in sorted order, with cleanup between
python manager/remote_cli.py --namespace NS run --scenario-dir /app/scenarios/joinmarket/test/batch_short
```

Paths passed to `run` are paths *inside* the orchestrator container (the repo is
baked into the image at `/app`), not paths on your machine.

Once a run is started, the remaining commands find it automatically — you do not
need to say whether it was a single run or a batch:

| Command | Effect |
| --- | --- |
| `status` | progress of the active run (batch: which scenario, how many done) |
| `logs -f` | stream the run's output |
| `skip` | batch only: abandon the current scenario, continue with the next |
| `stop` | stop the run (batch: the whole batch) |
| `download-logs -n 3` | fetch the last 3 finished simulation archives |
| `collect-logs` | break-glass: pull logs straight from the pods after a crash |

`collect-logs` exists for the case where the simulation died before it could
store its own logs. It talks only to `kubectl`, so it still works when the
normal log path is the thing that broke. Add `--blocks` to also dump the chain
(slow: one RPC per block).

> **Note:** finished run archives are written to `/app/logs` inside the
> orchestrator, which is *not* persistent — download them before the pod
> restarts. Only `/workspace` survives a restart.
