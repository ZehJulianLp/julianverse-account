#!/usr/bin/env python3
"""Copy the dependency-free browser integration into an app checkout (no deployment)."""

import argparse
import json
import shutil
from pathlib import Path

root = Path(__file__).resolve().parents[1]
parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument("app", choices=["startpage", "weather"])
parser.add_argument("checkout", type=Path)
parser.add_argument("--client-id", required=True)
args = parser.parse_args()
target = args.checkout.resolve() / "account"
target.mkdir(exist_ok=True)
for name in ("panel.mjs", "panel.css", "callback.mjs", "app.mjs", "data.mjs"):
    shutil.copyfile(root / "integrations/browser" / name, target / name)
for name in ("sync.mjs", "oidc-client.mjs"):
    shutil.copyfile(root / "account/static/js" / name, target / name)
adapter = (root / "integrations" / args.app / "adapter.mjs").read_text()
(target / "adapter.mjs").write_text(adapter.replace("../browser/data.mjs", "./data.mjs"))
(target / "config.mjs").write_text(
    "// Public OIDC client; contains no secrets.\nexport const config = "
    + json.dumps(
        {
            "issuer": "https://account.julianverse.de",
            "app": args.app,
            "clientId": args.client_id,
        },
        indent=2,
    )
    + ";\n"
)
shutil.copyfile(
    root / "integrations/browser/account-callback.html", args.checkout / "account-callback.html"
)
print(f"Copied {args.app} integration to {target}")
