# Twenty bounded decisions: inspect the gate before connecting a model

The offline demo replays recorded, synthetic answers through Jev Decision Layer's real local gates. For each of twenty shipped recipes, it checks a nominal, uncertain and adversarial case. No API key, provider call, workspace enrollment or confidential input is needed.

After the README quick start, run with Node.js 20 or newer:

```sh
node tools/demo.mjs
```

The final line is `PASS: 20 recipe examples, 60 synthetic gate checks`. Each preceding JSON row identifies the recipe, scenario, raw gate and experimental live-policy result. A recipe that has not been evaluated on real model outputs retains its verification requirement.

[Recorded output](launch/offline-demo-output.txt) is a reproducible gate-contract artifact. It is not a transcript of model inference, a prediction of accuracy, or a cost/latency benchmark. Run it yourself to confirm your checkout behaves the same way.

To evaluate a real decision, install for your host, approve a folder and provider in the private setup page, then use a public input and inspect its confidence and receipt. Review [host evidence](HOSTS.md) and [security boundaries](SECURITY.md). Hosted providers bill under their own terms. Local Laya needs Apple Silicon and a separately verified installation.
