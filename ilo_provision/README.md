# ilo_provision - DL380 Gen11 (iLO 6) CID provisioning

Source of truth: `cid_dl380_gen11_llx.json` (from CID BOA DL380 Gen11 LLx, rev 1.9).

```
set ILO_PASSWORD=...                       (or you will be prompted)
set MHSADMIN_PASSWORD=...                  (only needed if mhsadmin must be created)

python ilo_provision.py --ilo 10.0.0.100 -u admin --sku LLA --firmware-dir C:\fw            # PREVIEW
python ilo_provision.py --ilo 10.0.0.100 -u admin --sku LLA --firmware-dir C:\fw --apply    # apply
python ilo_provision.py --ilo 10.0.0.100 -u admin --sku LLA --firmware-dir C:\fw --apply --reboot
```
Run the preview again after the reboot - everything should read compliant.

Other options: `--only firmware,bios,boot,ilo`, `--show-inventory`, `--dump-bios file.json`,
`--reboot-type ForceRestart` (no OS running), `--security-state` (iLO HighSecurity, needs iLO reset),
`--ilo-static-ip/--ilo-netmask/--ilo-gateway` (needed before DHCPv4 is disabled).

Requires `pip install python-ilorest-library` (or leave the library folder next to this one - it is found automatically).
Offline self-test: `python test_ilo_provision.py`.
