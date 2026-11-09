#!/usr/bin/env python3
"""Catch a free Ampere A1 VM ("ocibox") in your OCI home region.

Runs inside GitHub Actions for a few minutes per run:
  * every ~60 s: ONE capacity-report call covering all fault domains x shapes
    -> if anything is AVAILABLE, launch there immediately
  * every 2nd cycle: one "blind" launch attempt (rotating fault domain / shape), because the
    capacity report is a snapshot and may not reflect the Always Free pool exactly
  * 429 "TooManyRequests" -> exponential back-off (never hammers the API)
Stops as soon as an instance named "ocibox" exists (from this run or any earlier one).

Prerequisite: oci/provision.sh has already created the network ("ocibox-subnet").
Everything it creates is Always Free: 2 OCPU/12 GB (or 1/6 fallback) A1, 200 GB boot volume.
"""
import argparse
import base64
import datetime as dt
import os
import pathlib
import sys
import time

import oci

NAME = "ocibox"
SHAPE = "VM.Standard.A1.Flex"
HERE = pathlib.Path(__file__).resolve().parent
NO_RETRY = oci.retry.NoneRetryStrategy()


def log(msg):
    print(f"{dt.datetime.now(dt.timezone.utc):%H:%M:%S}Z  {msg}", flush=True)


def gh_output(key, value):
    path = os.environ.get("GITHUB_OUTPUT")
    if path:
        with open(path, "a") as fh:
            fh.write(f"{key}={value}\n")


def gh_summary(text):
    path = os.environ.get("GITHUB_STEP_SUMMARY")
    if path:
        with open(path, "a") as fh:
            fh.write(text + "\n")


def env(name, required=True, default=None):
    v = os.environ.get(name, default)
    if required and not v:
        sys.exit(f"Missing secret/variable {name} - see README.md")
    return v


def mask(*values):
    if os.environ.get("GITHUB_ACTIONS"):
        for v in values:
            if v:
                for line in str(v).splitlines():
                    if line.strip():
                        print(f"::add-mask::{line.strip()}")


def parse_shapes(spec):
    out = []
    for part in spec.split(","):
        o, m = part.split(":")
        out.append((float(o), float(m)))
    return out


class Hunter:
    def __init__(self):
        key = env("OCI_PRIVATE_KEY").replace("\\n", "\n").strip() + "\n"
        self.cfg = {
            "user": env("OCI_USER_OCID"),
            "tenancy": env("OCI_TENANCY_OCID"),
            "fingerprint": env("OCI_FINGERPRINT"),
            "region": env("OCI_REGION"),
            "key_content": key,
        }
        mask(self.cfg["user"], self.cfg["tenancy"], self.cfg["fingerprint"], env("TS_AUTHKEY"))
        oci.config.validate_config(self.cfg)
        self.tenancy = self.cfg["tenancy"]
        self.comp = os.environ.get("OCI_COMPARTMENT_OCID") or self.tenancy
        mask(self.comp)
        self.compute = oci.core.ComputeClient(self.cfg)
        self.vnet = oci.core.VirtualNetworkClient(self.cfg)
        self.iam = oci.identity.IdentityClient(self.cfg)
        self.shapes = parse_shapes(os.environ.get("SHAPES") or "2:12,1:6")

    # ------------------------------------------------------------ discovery
    def existing(self):
        for i in self.compute.list_instances(self.comp, display_name=NAME).data:
            if i.lifecycle_state not in ("TERMINATED", "TERMINATING"):
                return i
        return None

    def prepare(self):
        subnets = [s for s in self.vnet.list_subnets(self.comp, display_name=f"{NAME}-subnet").data
                   if s.lifecycle_state == "AVAILABLE"]
        if not subnets:
            sys.exit("Subnet 'ocibox-subnet' not found. Run oci/provision.sh in Cloud Shell first "
                     "(it creates the network, then you can stop it while it's retrying).")
        self.subnet = subnets[0].id
        imgs = self.compute.list_images(self.comp, operating_system="Canonical Ubuntu",
                                        operating_system_version="24.04", shape=SHAPE,
                                        sort_by="TIMECREATED", sort_order="DESC").data
        imgs = [i for i in imgs if "Minimal" not in i.display_name]
        if not imgs:
            sys.exit("No Ubuntu 24.04 aarch64 image found.")
        self.image = imgs[0].id
        log(f"image {imgs[0].display_name}")
        self.ads = {}
        for ad in self.iam.list_availability_domains(self.tenancy).data:
            fds = [f.name for f in self.iam.list_fault_domains(self.tenancy, ad.name).data]
            self.ads[ad.name] = fds
        self.targets = [(ad, fd, o, m) for (o, m) in self.shapes for ad, fds in self.ads.items() for fd in fds]
        log(f"{len(self.targets)} targets: {sorted({t[1] for t in self.targets})} x shapes {self.shapes}")
        ci = (HERE.parent / "cloud-init.yaml").read_text().replace("__TS_AUTHKEY__", env("TS_AUTHKEY"))
        self.user_data = base64.b64encode(ci.encode()).decode()
        self.ssh_key = env("SSH_PUBKEY").strip()

    # ------------------------------------------------------------- actions
    def capacity(self):
        """{(ad, fd, ocpus, mem): status} from one report per AD."""
        result = {}
        for ad, fds in self.ads.items():
            items = [oci.core.models.CreateCapacityReportShapeAvailabilityDetails(
                        instance_shape=SHAPE, fault_domain=fd,
                        instance_shape_config=oci.core.models.CapacityReportInstanceShapeConfig(
                            ocpus=o, memory_in_gbs=m))
                     for (o, m) in self.shapes for fd in fds]
            rep = self.compute.create_compute_capacity_report(
                oci.core.models.CreateComputeCapacityReportDetails(
                    compartment_id=self.tenancy, availability_domain=ad, shape_availabilities=items),
                retry_strategy=NO_RETRY).data
            for sa in rep.shape_availabilities:
                cfg = sa.instance_shape_config
                result[(ad, sa.fault_domain, float(cfg.ocpus), float(cfg.memory_in_gbs))] = sa.availability_status
        return result

    def launch(self, ad, fd, ocpus, mem):
        d = oci.core.models.LaunchInstanceDetails(
            availability_domain=ad, compartment_id=self.comp, display_name=NAME, fault_domain=fd,
            shape=SHAPE,
            shape_config=oci.core.models.LaunchInstanceShapeConfigDetails(ocpus=ocpus, memory_in_gbs=mem),
            source_details=oci.core.models.InstanceSourceViaImageDetails(
                image_id=self.image, boot_volume_size_in_gbs=200),
            create_vnic_details=oci.core.models.CreateVnicDetails(
                subnet_id=self.subnet, assign_public_ip=True, hostname_label=NAME),
            metadata={"ssh_authorized_keys": self.ssh_key, "user_data": self.user_data},
            agent_config=oci.core.models.LaunchInstanceAgentConfigDetails(
                is_monitoring_disabled=False, is_management_disabled=False,
                plugins_config=[oci.core.models.InstanceAgentPluginConfigDetails(
                    name="Compute Instance Monitoring", desired_state="ENABLED")]))
        inst = self.compute.launch_instance(d, retry_strategy=NO_RETRY).data
        log(f"LAUNCH ACCEPTED in {fd} ({ocpus:g} OCPU / {mem:g} GB) - waiting for RUNNING...")
        try:
            inst = oci.wait_until(self.compute, self.compute.get_instance(inst.id),
                                  evaluate_response=lambda r: r.data.lifecycle_state in ("RUNNING", "TERMINATED"),
                                  max_wait_seconds=900, max_interval_seconds=15).data
        except oci.exceptions.MaximumWaitTimeExceeded:
            log("still provisioning after 15 min - treating as success (check the console)")
            return inst
        if inst.lifecycle_state == "TERMINATED":
            log("instance terminated during provisioning (capacity vanished) - continuing")
            return None
        return inst

    # ----------------------------------------------------------------- loop
    def run(self, minutes):
        found = self.existing()
        if found:
            return self.done(found, already=True)
        self.prepare()
        deadline = time.time() + minutes * 60
        cycle, delay, blind_idx = 0, 60, 0
        while time.time() < deadline:
            cycle += 1
            try:
                status = self.capacity()
                avail = [k for k, v in status.items() if v == "AVAILABLE"]
                summary = ", ".join(f"{k[1].split('-')[-1]}/{k[2]:g}:{v}" for k, v in sorted(status.items()))
                log(f"capacity: {summary}")
                order = sorted(avail, key=lambda k: -k[2])  # prefer the bigger shape
                if not order and cycle % 2 == 0:
                    order = [self.targets[blind_idx % len(self.targets)]]
                    blind_idx += 1
                    log(f"blind attempt {order[0][1]} {order[0][2]:g}/{order[0][3]:g}")
                for ad, fd, o, m in order:
                    try:
                        inst = self.launch(ad, fd, o, m)
                        if inst:
                            return self.done(inst)
                    except oci.exceptions.ServiceError as e:
                        if e.status == 429:
                            raise
                        if "capacity" in (e.message or "").lower():
                            log(f"  {fd} {o:g}/{m:g}: out of host capacity")
                        elif e.code == "LimitExceeded":
                            log(f"  LimitExceeded: {e.message}")
                            found = self.existing()
                            if found:
                                return self.done(found, already=True)
                        else:
                            log(f"  launch error {e.status} {e.code}: {e.message}")
                delay = 60
            except oci.exceptions.ServiceError as e:
                if e.status == 429:
                    delay = min(delay * 2, 300)
                    log(f"429 TooManyRequests - backing off {delay}s")
                elif e.status >= 500:
                    log(f"OCI {e.status} {e.code} - retrying")
                else:
                    log(f"OCI error {e.status} {e.code}: {e.message}")
                    if e.status in (401, 404):
                        sys.exit("Authentication/permission problem - check the secrets (README).")
            remaining = deadline - time.time()
            if remaining <= 0:
                break
            time.sleep(min(delay, remaining))
        log("no capacity this run - the next scheduled run continues")
        gh_output("got_vm", "false")
        return 0

    def done(self, inst, already=False):
        shape = inst.shape_config
        msg = (f"{'Found existing' if already else 'GOT IT:'} instance '{NAME}' "
               f"({shape.ocpus:g} OCPU / {shape.memory_in_gbs:g} GB) in {inst.fault_domain}, state {inst.lifecycle_state}")
        log(msg)
        gh_output("got_vm", "true")
        gh_output("shape", f"{shape.ocpus:g} OCPU / {shape.memory_in_gbs:g} GB")
        gh_summary(f"### {msg}\n\nNext: in OCI Cloud Shell run `./provision.sh` again - it finds the VM and finishes setup.")
        return 0


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--minutes", type=float, default=12)
    sys.exit(Hunter().run(ap.parse_args().minutes))
