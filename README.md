# oci-a1-hunter

GitHub Actions job that keeps trying to create the free **Ampere A1** VM (`ocibox`) in an
Oracle Cloud home region that's "out of host capacity", even while your laptop is off.

- Runs every 15 minutes and hunts for ~12 minutes each time.
- Each minute it makes **one** capacity-check call covering all fault domains and sizes, and launches immediately
  when a slot shows as available. Every other minute it also makes one plain launch attempt.
- Tries **2 OCPU / 12 GB** first, then **1 OCPU / 6 GB**. You can resize to 2/12 later in the console.
- Backs off automatically on Oracle's "429 TooManyRequests".
- **Stops itself** once the VM exists, and opens a GitHub issue so you get an email.
- Creates only Always Free resources: A1 within the free size, 200 GB boot disk, in the subnet made by `provision.sh`.

Logs show only times, fault domains and capacity status. OCIDs and keys are masked.

## Setup (10 minutes)

**0. Prerequisite:** you already ran `provision.sh` in Cloud Shell at least once, so the network
(`ocibox-subnet`) exists. You can stop it (Ctrl+C) while it's stuck retrying.

**1. Create an OCI API key**
OCI console → profile icon (top right) → **My profile** → **API keys** → **Add API key** →
**Generate API key pair** → **Download private key** → **Add**.
A "Configuration file preview" appears. Keep it open; you need the `user`, `fingerprint`,
`tenancy` and `region` values from it.

**2. Add repository secrets:** Settings → Secrets and variables → Actions → **Secrets** → New repository secret

| Name | Value |
|---|---|
| `OCI_USER_OCID` | `user=` value from the preview |
| `OCI_TENANCY_OCID` | `tenancy=` value |
| `OCI_FINGERPRINT` | `fingerprint=` value |
| `OCI_PRIVATE_KEY` | Full contents of the downloaded `.pem` file, including the BEGIN/END lines |
| `TS_AUTHKEY` | Your Tailscale auth key (`tskey-auth-…`). Use a **90-day** key so it doesn't expire while hunting. |

**3. Add repository variables:** same page → **Variables** tab

| Name | Value |
|---|---|
| `OCI_REGION` | `ap-mumbai-1` |
| `SSH_PUBKEY` | Your laptop's public key line (`ssh-ed25519 AAAA… 12345@AsusTUFA15`) |
| `SHAPES` | *(optional)* default `2:12,1:6`. Use `2:12` to only accept the full size. |

**4. Start it:** Actions tab → **a1-hunter** → **Run workflow**. After that it runs on its own every 15 minutes.

## When it succeeds

1. You get an email from the GitHub issue it opens. The machine **ocibox** also appears in Tailscale.
   In the Tailscale admin console choose **Disable key expiry** for it.
2. In OCI Cloud Shell run `./provision.sh` again. It finds the VM and finishes the rest:
   backups, alarms, Email Delivery, the dynamic group.
3. **Delete the API key** you made in step 1 (My profile → API keys) and the repo secrets. They're no longer needed.
4. If you got 1 OCPU / 6 GB: later, Compute → Instances → ocibox → **Edit** → shape → 2 OCPU / 12 GB.
   This also needs free capacity, so retry occasionally.

## Notes

- **Public repo:** Actions minutes are free and unlimited on public repos, but limited (2,000 min/month) on private
  ones, and this job needs far more than that. Nothing secret is in the code; credentials live in encrypted
  repository secrets, which GitHub never exposes to forks or logs.
- **The API key is powerful** (it acts as your OCI user). It's only stored in GitHub's encrypted secrets.
  Delete it as soon as the VM exists.
- To pause: Actions → a1-hunter → ⋯ → **Disable workflow**.
