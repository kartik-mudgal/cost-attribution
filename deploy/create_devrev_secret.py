"""Create or update the DevRev PAT secret for the cost investigator runtime.

The token is read from the DEVREV_PAT environment variable (not from argv).

Usage:
  export DEVREV_PAT='...'
  AWS_PROFILE=hackathon python deploy/create_devrev_secret.py

Writes deploy/build/devrev-secret.json with the secret ARN (for deploy_runtime.py).
"""
import json
import os
import pathlib

import boto3

REGION = "us-west-2"
ACCOUNT = "270149200236"
SECRET_NAME = "cost-agent-devrev-pat"
BUILD = pathlib.Path(__file__).resolve().parent / "build"


def main() -> None:
    token = os.environ.get("DEVREV_PAT", "").strip()
    if not token:
        raise SystemExit("Set DEVREV_PAT to the DevRev personal access token (JWT).")

    client = boto3.client("secretsmanager", region_name=REGION)
    secret_string = json.dumps({"pat": token})
    try:
        resp = client.create_secret(Name=SECRET_NAME, SecretString=secret_string)
        arn = resp["ARN"]
    except client.exceptions.ResourceExistsException:
        resp = client.describe_secret(SecretId=SECRET_NAME)
        arn = resp["ARN"]
        client.put_secret_value(SecretId=SECRET_NAME, SecretString=secret_string)

    BUILD.mkdir(parents=True, exist_ok=True)
    out = {"secret_arn": arn, "secret_name": SECRET_NAME, "json_key": "pat"}
    (BUILD / "devrev-secret.json").write_text(json.dumps(out, indent=2))
    print(json.dumps(out, indent=2))


if __name__ == "__main__":
    main()
