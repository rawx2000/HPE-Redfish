#!/usr/bin/env python3
#1.1version
"""
ilo_provision.py - bring an HPE DL380 Gen11 (iLO 6) in line with the Customer Intent Document.

  Default            PREVIEW only. Reads the server, prints what would change. Nothing is written.
  --apply            Make the changes (firmware upload/flash, BIOS, boot order, iLO settings).
  --apply --reboot   ...and then restart the server so pending BIOS changes / queued firmware take effect.

The script is idempotent: run it again in preview mode afterwards and everything should read "compliant".

Credentials: iLO password from env ILO_PASSWORD (or prompted). Never taken from the command line.
             The mhsadmin password is read from env MHSADMIN_PASSWORD when that account has to be created.
"""
import argparse
import getpass
import json
import os
import re
import sys
import time
import zipfile

HERE = os.path.dirname(os.path.abspath(__file__))
VERSION = "1.9.1"   # keep in step with "_version" in cid_dl380_gen11_llx.json


def _import_redfish():
    try:
        import redfish
        return redfish
    except ImportError:
        # fall back to the library source that sits next to this script (HPE Settings folder layout)
        for rel in ("python-ilorest-library-master/python-ilorest-library-master/src",
                    "../python-ilorest-library-master/python-ilorest-library-master/src"):
            p = os.path.normpath(os.path.join(HERE, rel))
            if os.path.isdir(p):
                sys.path.insert(0, p)
        import redfish
        return redfish


# --------------------------------------------------------------------------- helpers
OK, CHANGE, UNRESOLVED, SKIP, MANUAL, PENDING = "OK", "CHANGE", "UNRESOLVED", "SKIP", "MANUAL", "PENDING"
SYMBOL = {OK: "=", CHANGE: "~", UNRESOLVED: "?", SKIP: "-", MANUAL: "-", PENDING: "p"}


class ApplyError(Exception):
    pass


class MissingParam(Exception):
    pass


class Item:
    """One planned unit of work."""

    def __init__(self, section, title, status, lines=None, run=None, unresolved=0, late=False):
        self.section, self.title, self.status = section, title, status
        self.lines = lines or []
        self.run = run
        self.unresolved = unresolved   # count of sub-settings that could not be mapped
        self.late = late
        self.error = None


def dig(d, *keys):
    for k in keys:
        if not isinstance(d, dict) or k not in d:
            return None
        d = d[k]
    return d


def norm(s):
    return re.sub(r"[^a-z0-9]", "", str(s).lower())


def _scalar(v):
    if v is None:
        return ""
    if isinstance(v, bool):
        return str(v).lower()
    return str(v).strip().lower()


def same(cur, des):
    if isinstance(des, list) and isinstance(cur, list):
        c, d = list(cur), list(des)
        while c and _scalar(c[-1]) == "" and not isinstance(c[-1], (dict, list)):
            c.pop()
        while d and _scalar(d[-1]) == "" and not isinstance(d[-1], (dict, list)):
            d.pop()
        return len(c) == len(d) and all(same(a, b) for a, b in zip(c, d))
    if isinstance(des, dict) and isinstance(cur, dict):
        return all(k in cur and same(cur[k], v) for k, v in des.items())
    return _scalar(cur) == _scalar(des)


def diff(desired, current, path=""):
    """Compare a desired (partial) body with the live resource.
    Returns (changes[(path, cur, des)], missing[path], patch_body)."""
    changes, missing, body = [], [], {}
    for k, v in desired.items():
        p = "%s/%s" % (path, k) if path else k
        if not isinstance(current, dict) or k not in current:
            missing.append(p)
            continue
        if isinstance(v, dict):
            c, m, b = diff(v, current[k], p)
            changes += c
            missing += m
            if b:
                body[k] = b
        elif not same(current[k], v):
            changes.append((p, current[k], v))
            body[k] = v
    return changes, missing, body


def short(v, n=70):
    s = json.dumps(v) if not isinstance(v, str) else v
    return s if len(s) <= n else s[: n - 3] + "..."


def subst(obj, params):
    if isinstance(obj, dict):
        return {k: subst(v, params) for k, v in obj.items()}
    if isinstance(obj, list):
        return [subst(v, params) for v in obj]
    if isinstance(obj, str):
        m = re.fullmatch(r"\$\{(\w+)\}", obj)
        if m:
            if not params.get(m.group(1)):
                raise MissingParam(m.group(1))
            return params[m.group(1)]
    return obj


def check(resp, what):
    if resp.status in (200, 201, 202, 204):
        return resp
    msg = ""
    try:
        info = resp.dict["error"]["@Message.ExtendedInfo"]
        msg = "; ".join(i.get("MessageId", "") + " " + i.get("Message", "") for i in info)
    except Exception:
        pass
    raise ApplyError("%s failed: HTTP %s %s" % (what, resp.status, msg))


# --------------------------------------------------------------------------- iLO access
class Ilo:
    def __init__(self, connect):
        self._connect = connect
        self.c = connect()
        self.cache = {}

    def reconnect(self, timeout=900):
        t0 = time.time()
        try:
            self.c.logout()
        except Exception:
            pass
        while True:
            try:
                self.c = self._connect()
                self.cache.clear()
                return
            except Exception as e:
                if time.time() - t0 > timeout:
                    raise ApplyError("iLO did not come back within %ds (%s)" % (timeout, e))
                print("    waiting for iLO ...")
                time.sleep(20)

    def get(self, uri, fresh=False):
        if not uri:
            return None
        if fresh or uri not in self.cache:
            r = self.c.get(uri)
            self.cache[uri] = r.dict if r.status == 200 else None
        return self.cache[uri]

    def first_member(self, uri):
        coll = self.get(uri)
        if coll and coll.get("Members"):
            return coll["Members"][0]["@odata.id"]

    @property
    def root(self):
        return self.get("/redfish/v1/")

    def manager(self):
        return self.first_member(dig(self.root, "Managers", "@odata.id") or "/redfish/v1/Managers/")

    def system(self):
        return self.first_member(dig(self.root, "Systems", "@odata.id") or "/redfish/v1/Systems/")

    def hpe_link(self, *names):
        links = dig(self.get(self.manager()), "Oem", "Hpe", "Links") or dig(self.get(self.manager()), "Oem", "Hp", "Links") or {}
        for n in names:
            if n in links:
                return links[n].get("@odata.id")

    def resolve(self, key):
        m, s = self.get(self.manager()) or {}, self.get(self.system()) or {}
        table = {
            "manager": lambda: self.manager(),
            "system": lambda: self.system(),
            "network_protocol": lambda: dig(m, "NetworkProtocol", "@odata.id"),
            "network_service": lambda: self.hpe_link("NetworkService"),
            "snmp": lambda: self.hpe_link("Snmp", "SNMPService"),
            "datetime": lambda: self.hpe_link("DateTimeService"),
            "alertmail": lambda: self.hpe_link("AlertMailService"),
            "license": lambda: self.hpe_link("LicenseService"),
            "security_service": lambda: self.hpe_link("SecurityService"),
            "login_banner": lambda: (self.hpe_link("LoginSecurityBanner")
                                     or ((self.hpe_link("SecurityService") or "").rstrip("/") + "/LoginSecurityBanner/")),
            "account_service": lambda: dig(self.root, "AccountService", "@odata.id"),
            "update_service": lambda: dig(self.root, "UpdateService", "@odata.id"),
            "secure_boot": lambda: dig(s, "SecureBoot", "@odata.id"),
            "bios": lambda: dig(s, "Bios", "@odata.id"),
            "ethernet_dedicated": self._dedicated_nic,
        }
        fn = table.get(key)
        return fn() if fn else None

    def _dedicated_nic(self):
        coll = self.get(dig(self.get(self.manager()), "EthernetInterfaces", "@odata.id"))
        members = [x["@odata.id"] for x in (coll or {}).get("Members", [])]
        for uri in members:
            n = self.get(uri) or {}
            if "dedicated" in ("%s %s" % (n.get("Name", ""), n.get("Description", ""))).lower():
                return uri
        return members[0] if members else None


# --------------------------------------------------------------------------- generic settings
def plan_generic(ilo, section, spec, params):
    title = spec["name"]
    try:
        body = subst(spec["body"], params)
    except MissingParam as e:
        return Item(section, title, SKIP, ["needs --%s" % str(e).lower()])
    cands = []
    for r in spec["resources"]:
        uri = ilo.resolve(r)
        cur = ilo.get(uri) if uri else None
        if cur is None:
            continue
        ch, miss, patch = diff(body, cur)
        cands.append((len(miss), uri, ch, miss, patch))
    if not cands:
        return Item(section, title, UNRESOLVED, ["resource %s not found on this iLO" % spec["resources"]], unresolved=1)
    nmiss, uri, ch, miss, patch = min(cands, key=lambda c: c[0])
    lines = ["%s: %s -> %s" % (p, short(c), short(d)) for p, c, d in ch]
    lines += ["? %s: not present at %s (property name differs on this firmware?)" % (p, uri) for p in miss]
    if patch:
        def run(uri=uri, patch=patch, title=title):
            check(ilo.c.patch(uri, patch), title)
            ilo.cache.pop(uri, None)
        return Item(section, title, CHANGE, lines, run, unresolved=nmiss)
    return Item(section, title, UNRESOLVED if nmiss else OK, lines, unresolved=nmiss)


def plan_timezone(ilo, spec):
    uri = ilo.resolve("datetime")
    dt = ilo.get(uri) if uri else None
    if not dt:
        return Item("ilo", spec["name"], UNRESOLVED, ["DateTime resource not found"], unresolved=1)
    want = [w.lower() for w in spec["match"]]
    names = [z.get("Name", "") for z in dt.get("TimeZoneList", [])]
    hit = [n for n in names if all(w in n.lower() for w in want)]
    if len(hit) != 1:
        return Item("ilo", spec["name"], UNRESOLVED,
                    ["%d time zones match %s; candidates: %s" % (len(hit), spec["match"], [n for n in names if want[0] in n.lower()][:6])],
                    unresolved=1)
    cur = dig(dt, "TimeZone", "Name")
    if cur == hit[0]:
        return Item("ilo", spec["name"], OK)

    def run():
        check(ilo.c.patch(uri, {"TimeZone": {"Name": hit[0]}}), "time zone")
        ilo.cache.pop(uri, None)
    return Item("ilo", spec["name"], CHANGE, ["TimeZone: %s -> %s" % (cur, hit[0])], run)


def plan_account(ilo, spec):
    coll = dig(ilo.get("/redfish/v1/AccountService/") or {}, "Accounts", "@odata.id") or "/redfish/v1/AccountService/Accounts/"
    members = (ilo.get(coll) or {}).get("Members", [])
    for m in members:
        if (ilo.get(m["@odata.id"]) or {}).get("UserName", "").lower() == spec["username"].lower():
            return Item("ilo", spec["name"], OK, ["exists - password is never changed by this script"])

    def run():
        pw = os.environ.get(spec["password_env"])
        if not pw:
            raise ApplyError("environment variable %s is not set" % spec["password_env"])
        check(ilo.c.post(coll, {"UserName": spec["username"], "Password": pw, "RoleId": spec["role"]}), "create user")
    note = "create %s (role %s); password from $%s%s" % (
        spec["username"], spec["role"], spec["password_env"], "" if os.environ.get(spec["password_env"]) else "  ** NOT SET **")
    return Item("ilo", spec["name"], CHANGE, [note], run, late=True)


def plan_license(ilo, spec):
    uri = ilo.resolve("license")
    svc = ilo.get(uri) if uri else None
    if not svc:
        return Item("ilo", spec["name"], UNRESOLVED, ["LicenseService not found"], unresolved=1)
    tail = spec["key"][-5:].upper()
    for m in svc.get("Members", []):
        mem = ilo.get(m["@odata.id"]) or {}
        if str(mem.get("LicenseKey", "")).upper().endswith(tail):
            return Item("ilo", spec["name"], OK, ["key ending %s already installed" % tail])

    def run():
        check(ilo.c.post(uri, {"LicenseKey": spec["key"]}), "licence key")
    return Item("ilo", spec["name"], CHANGE, ["install key ending %s" % tail], run, late=True)


def plan_static_ip(ilo, spec, args):
    uri = ilo.resolve("ethernet_dedicated")
    nic = ilo.get(uri) if uri else None
    if not nic:
        return Item("ilo", spec["name"], UNRESOLVED, ["dedicated NIC not found"], unresolved=1)
    dhcp = dig(nic, "DHCPv4", "DHCPEnabled")
    if dhcp is False and not args.ilo_static_ip:
        return Item("ilo", spec["name"], OK, ["DHCPv4 already disabled"])
    if not args.ilo_static_ip:
        return Item("ilo", spec["name"], MANUAL,
                    ["CID says DHCPv4 = Disabled but gives no address. Not changed - disabling DHCP without a static "
                     "address would lock you out. Re-run with --ilo-static-ip/--ilo-netmask/--ilo-gateway."])
    body = {"DHCPv4": {"DHCPEnabled": False},
            "IPv4Addresses": [{"Address": args.ilo_static_ip, "SubnetMask": args.ilo_netmask,
                               "Gateway": args.ilo_gateway, "AddressOrigin": "Static"}]}
    if not (args.ilo_netmask and args.ilo_gateway):
        return Item("ilo", spec["name"], UNRESOLVED, ["--ilo-static-ip needs --ilo-netmask and --ilo-gateway"], unresolved=1)

    def run():
        try:
            check(ilo.c.patch(uri, body), "static IPv4")
        except ApplyError:
            raise
        except Exception as e:     # connection normally drops when the address changes
            print("    note: connection dropped while changing the address (%s) - verify on the new IP" % e)
    return Item("ilo", spec["name"], CHANGE,
                ["DHCPv4 -> disabled; static %s / %s gw %s (may drop this session)" % (args.ilo_static_ip, args.ilo_netmask, args.ilo_gateway)],
                run, late=True)


def plan_security_state(ilo, spec, args):
    uri = ilo.resolve("security_service")
    svc = ilo.get(uri) if uri else None
    cur = (svc or {}).get("SecurityState")
    if cur is None:
        return Item("ilo", spec["name"], UNRESOLVED, ["SecurityState not readable"], unresolved=1)
    if norm(cur) == norm(spec["value"]):
        return Item("ilo", spec["name"], OK)
    if not args.security_state:
        return Item("ilo", spec["name"], MANUAL,
                    ["%s -> %s is NOT applied unless you pass --security-state (changes crypto/TLS behaviour and "
                     "needs an iLO reset)" % (cur, spec["value"])])

    def run():
        check(ilo.c.patch(uri, {"SecurityState": spec["value"]}), "security state")
        print("    note: iLO reset required for the new security state to take effect")
    return Item("ilo", spec["name"], CHANGE, ["SecurityState: %s -> %s (iLO reset needed)" % (cur, spec["value"])], run, late=True)


def plan_ldap_groups(ilo, spec):
    """Make sure the directory groups exist. Existing mappings (and the privileges iLO stores for them as
    custom roles) are never replaced - only missing groups are appended."""
    uri = ilo.resolve("account_service")
    cur = dig(ilo.get(uri) or {}, "LDAP", "RemoteRoleMapping")
    if cur is None:
        return Item("ilo", spec["name"], UNRESOLVED, ["LDAP.RemoteRoleMapping not readable"], unresolved=1)
    have = {str(m.get("RemoteGroup", "")).lower() for m in cur}
    missing = []
    for g in spec["groups"]:
        cn = re.match(r"(?i)cn=([^,]+)", g["dn"])
        names = {g["dn"].lower()} | ({cn.group(1).lower()} if cn else set())
        if not (names & have):
            missing.append(g)
    lines = ["%d of %d groups present; their privileges are NOT compared (stored by iLO as custom roles) - verify in the iLO UI"
             % (len(spec["groups"]) - len(missing), len(spec["groups"]))]
    if not missing:
        return Item("ilo", spec["name"], OK, lines)
    lines += ["add %s -> %s" % (g["dn"], g["role"]) for g in missing]
    new = list(cur) + [{"LocalRole": g["role"], "RemoteGroup": g["dn"]} for g in missing]

    def run():
        check(ilo.c.patch(uri, {"LDAP": {"RemoteRoleMapping": new}}), "directory groups")
    return Item("ilo", spec["name"], CHANGE, lines, run)


def plan_ilo(ilo, cfg, args, params):
    items = []
    for spec in cfg["ilo"]:
        t = spec.get("type")
        if t == "timezone":
            items.append(plan_timezone(ilo, spec))
        elif t == "ldap_groups":
            items.append(plan_ldap_groups(ilo, spec))
        elif t == "accounts":
            items.append(plan_account(ilo, spec))
        elif t == "license":
            items.append(plan_license(ilo, spec))
        elif t == "static_ip":
            items.append(plan_static_ip(ilo, spec, args))
        elif t == "security_state":
            items.append(plan_security_state(ilo, spec, args))
        else:
            items.append(plan_generic(ilo, "ilo", spec, params))
    return items


# --------------------------------------------------------------------------- BIOS
def load_registry(ilo, bios):
    reg_id = (bios or {}).get("AttributeRegistry")
    if not reg_id:
        return {}
    for m in (ilo.get("/redfish/v1/Registries/") or {}).get("Members", []):
        if reg_id in m["@odata.id"]:
            for loc in (ilo.get(m["@odata.id"]) or {}).get("Location", []):
                if loc.get("Uri"):
                    doc = ilo.get(loc["Uri"]) or {}
                    return {e["AttributeName"]: e for e in dig(doc, "RegistryEntries", "Attributes") or []}
    return {}


def resolve_attr(label, hint, menu, reg, current):
    by_name = [n for n, e in reg.items() if norm(e.get("DisplayName")) == norm(label)]
    if menu:
        by_name = [n for n in by_name if menu.lower() in str(reg[n].get("MenuPath", "")).lower()]
    if hint and hint in reg and norm(reg[hint].get("DisplayName")) == norm(label):
        return hint, None
    if len(by_name) == 1:
        return by_name[0], None
    if len(by_name) > 1:
        return None, "ambiguous: %s (add a \"menu\" filter)" % ", ".join("%s [%s]" % (n, reg[n].get("MenuPath", "")) for n in by_name[:4])
    if hint and (hint in reg or (not reg and hint in current)):
        return hint, None
    return None, "no attribute named '%s'%s on this BIOS" % (label, " / %s" % hint if hint else "")


def resolve_value(entry, value):
    if not entry:
        return value, None
    t = entry.get("Type")
    if t == "Enumeration":
        opts = entry.get("Value", [])
        for o in opts:
            if norm(value) in (norm(o.get("ValueName")), norm(o.get("ValueDisplayName"))):
                return o["ValueName"], None
        return None, "value '%s' not allowed; choices: %s" % (
            value, ", ".join("%s (%s)" % (o.get("ValueName"), o.get("ValueDisplayName")) for o in opts))
    if t == "Integer":
        try:
            iv = int(value)
        except (TypeError, ValueError):
            return None, "'%s' is not an integer" % value
        lo, hi = entry.get("LowerBound"), entry.get("UpperBound")
        if (lo is not None and iv < lo) or (hi is not None and iv > hi):
            return None, "%s outside allowed range %s..%s" % (iv, lo, hi)
        return iv, None
    return value, None


def plan_bios(ilo, cfg):
    bios_uri = ilo.resolve("bios")
    bios = ilo.get(bios_uri) if bios_uri else None
    if not bios:
        return [Item("bios", "BIOS", UNRESOLVED, ["Bios resource not found"], unresolved=1)]
    cur = bios.get("Attributes", {})
    settings_uri = dig(bios, "@Redfish.Settings", "SettingsObject", "@odata.id")
    pending = (ilo.get(settings_uri, fresh=True) or {}).get("Attributes", {}) if settings_uri else {}
    reg = load_registry(ilo, bios)
    lines, patch, unresolved, ok, pend = [], {}, 0, 0, 0
    todo = [(r[0], r[1], r[2], (r[3] if len(r) > 3 else {}).get("menu")) for r in cfg["bios"]]

    for rule in cfg.get("bios_rules", []):
        rx = re.compile(rule["regex"])
        names = sorted(n for n in cur if rx.search(n))     # only attributes this system actually exposes
        if not names:
            lines.append("? rule '%s': no matching attributes on this BIOS" % rule["label"])
            unresolved += 1
        for n in names:
            todo.append(("%s [%s]" % (rule["label"], n), n, rule["except"].get(n, rule["value"]), None))

    for label, hint, value, menu in todo:
        attr, err = resolve_attr(label, hint, menu, reg, cur)
        if not attr:
            lines.append("? %s: %s" % (label, err)); unresolved += 1; continue
        entry = reg.get(attr)
        if entry and entry.get("ReadOnly"):
            lines.append("- %s (%s): read-only here (profile-controlled?), currently %s" % (label, attr, short(cur.get(attr)))); continue
        if attr not in cur:
            lines.append("- %s (%s): not exposed on this system/config" % (label, attr)); continue
        val, err = resolve_value(entry, value)
        if err:
            lines.append("? %s (%s): %s" % (label, attr, err)); unresolved += 1; continue
        if same(cur[attr], val):
            ok += 1
        elif attr in pending and same(pending[attr], val):
            pend += 1
        else:
            patch[attr] = val
            lines.append("%s (%s): %s -> %s" % (label, attr, short(cur[attr]), short(val)))
    if not reg:
        lines.append("? attribute registry not readable - names/values were NOT validated")
        unresolved += 1
    lines.append("(%d already compliant, %d pending reboot)" % (ok, pend))

    run = None
    if patch:
        def run():
            check(ilo.c.patch(settings_uri, {"Attributes": patch}), "BIOS settings")
    status = CHANGE if patch else (PENDING if pend else (UNRESOLVED if unresolved else OK))
    return [Item("bios", "BIOS attributes (%d to change)" % len(patch), status, lines, run, unresolved=unresolved)]


def plan_boot(ilo, cfg):
    sys_uri = ilo.system()
    system = ilo.get(sys_uri) or {}
    boot = system.get("Boot", {})
    cur_order = boot.get("BootOrder")
    opts_uri = dig(boot, "BootOptions", "@odata.id")
    if cur_order is None or not opts_uri:
        return [Item("boot", "UEFI boot order", UNRESOLVED, ["Boot.BootOrder / BootOptions not available"], unresolved=1)]
    options = {}
    for m in (ilo.get(opts_uri) or {}).get("Members", []):
        o = ilo.get(m["@odata.id"]) or {}
        options[o.get("BootOptionReference", o.get("Id"))] = "%s %s" % (o.get("DisplayName", ""), o.get("UefiDevicePath", ""))
    want, lines, unresolved = [], [], 0
    for spec in cfg["boot_order"]:
        hit = [r for r, text in options.items() if all(w in text.lower() for w in spec["match"]) and r not in want]
        if len(hit) == 1:
            want.append(hit[0])
        else:
            lines.append("? %s: %d boot entries match %s" % (spec["name"], len(hit), spec["match"])); unresolved += 1
    if unresolved:
        lines.append("available: " + "; ".join("%s=%s" % (r, t.strip()[:60]) for r, t in options.items()))
        return [Item("boot", "UEFI boot order", UNRESOLVED, lines, unresolved=unresolved)]
    new_order = want + [r for r in cur_order if r not in want]
    lines.append("current: %s" % [options.get(r, r)[:40] for r in cur_order])
    lines.append("desired: %s" % [options.get(r, r)[:40] for r in new_order])
    if new_order == cur_order:
        return [Item("boot", "UEFI boot order", OK)]

    def run():
        check(ilo.c.patch(sys_uri, {"Boot": {"BootOrder": new_order}}), "boot order")
    return [Item("boot", "UEFI boot order", CHANGE, lines, run)]


# --------------------------------------------------------------------------- firmware
def numtokens(s):
    return {t.lstrip("0") or "0" for t in re.findall(r"\d+", str(s))}


def version_ok(installed, target):
    return numtokens(target) <= numtokens(installed)


def groups_match(groups, text):
    text = text.lower()
    return any(all(w in text for w in g) for g in groups)


def fw_inventory(ilo):
    us = ilo.get(ilo.resolve("update_service")) or {}
    inv_uri = dig(us, "FirmwareInventory", "@odata.id")
    out = []
    for m in (ilo.get(inv_uri) or {}).get("Members", []):
        d = ilo.get(m["@odata.id"]) or {}
        out.append({"name": d.get("Name", ""), "id": d.get("Id", ""), "version": str(d.get("Version", "")),
                    "text": "%s %s %s" % (d.get("Name", ""), d.get("Id", ""), d.get("Description", ""))})
    return out


def scan_packages(fw_dir):
    pk = []
    if not fw_dir:
        return pk
    for fn in sorted(os.listdir(fw_dir)):
        path = os.path.join(fw_dir, fn)
        if not os.path.isfile(path) or not fn.lower().endswith((".fwpkg", ".flash", ".bin", ".exe", ".rpm")):
            continue
        meta = {}
        if zipfile.is_zipfile(path):
            try:
                with zipfile.ZipFile(path) as z:
                    meta = json.loads(z.read("payload.json").decode("utf-8-sig"))
            except Exception:
                meta = {}
        devs = " ".join(str(d.get("DeviceName", "")) for d in dig(meta, "Devices", "Device") or [])
        pk.append({"path": path, "file": fn, "version": str(meta.get("FirmwareVersion", "")),
                   "text": "%s %s %s" % (fn, meta.get("Description", ""), devs),
                   "compsig": os.path.splitext(path)[0] + ".compsig"})
    return pk


def upload_package(ilo, pkg, label):
    us = ilo.get(ilo.resolve("update_service"), fresh=True)
    push = us.get("HttpPushUri")
    if not push:
        raise ApplyError("UpdateService has no HttpPushUri")
    key = ilo.c.session_key
    params = json.dumps({"UpdateRepository": True, "UpdateTarget": True, "ETag": "ilo_provision", "Section": 0})
    with open(pkg["path"], "rb") as f:
        data = f.read()
    body = [("sessionKey", key), ("parameters", params)]
    if os.path.exists(pkg["compsig"]):
        with open(pkg["compsig"], "rb") as f:
            body.append(("compsig", (os.path.basename(pkg["compsig"]), f.read(), "application/octet-stream")))
    body.append(("file", (pkg["file"], data, "application/octet-stream")))
    print("    uploading %s (%.1f MB) ..." % (pkg["file"], len(data) / 1e6))
    check(ilo.c.post(push, body, headers={"Cookie": "sessionKey=" + key}), "upload " + label)


def wait_update_idle(ilo, timeout=2400):
    t0 = time.time()
    while time.time() - t0 < timeout:
        try:
            st = dig(ilo.get(ilo.resolve("update_service"), fresh=True), "Oem", "Hpe", "State")
            if st in (None, "Idle", "Complete", "Error", "Failed", "ERROR"):
                return st
        except Exception:
            ilo.reconnect()
        time.sleep(10)
    raise ApplyError("firmware update did not finish within %d s" % timeout)


def plan_firmware(ilo, cfg, args):
    inv = fw_inventory(ilo)
    pkgs = scan_packages(args.firmware_dir)
    if args.show_inventory:
        print("Firmware inventory:")
        for i in inv:
            print("   %-60s %s" % (i["name"][:60], i["version"]))
    items = []
    for comp in cfg["firmware"]:
        hits = [i for i in inv if groups_match(comp["inventory_match"], i["text"])]
        if not hits:
            items.append(Item("firmware", comp["name"], SKIP, ["not present in firmware inventory (component not installed?)"]))
            continue
        stale = [i for i in hits if not version_ok(i["version"], comp["target"])]
        raw = ["%s: %s%s" % (i["name"][:50], i["version"], "" if i not in stale else "  -> needs %s" % comp["target"]) for i in hits]
        lines = ["%s%s" % (l, "  (x%d)" % raw.count(l) if raw.count(l) > 1 else "") for l in dict.fromkeys(raw)]
        if not stale:
            items.append(Item("firmware", comp["name"], OK, lines)); continue
        cand = [p for p in pkgs if groups_match(comp["package_match"], p["text"])]
        if len(cand) != 1:
            lines.append("? %d firmware packages in --firmware-dir match %s (need exactly 1)" % (len(cand), comp["package_match"]))
            items.append(Item("firmware", comp["name"], UNRESOLVED, lines, unresolved=1)); continue
        pkg = cand[0]
        if pkg["version"] and not version_ok(pkg["version"], comp["target"]):
            lines.append("? package %s is version %s, baseline is %s - not flashing" % (pkg["file"], pkg["version"], comp["target"]))
            items.append(Item("firmware", comp["name"], UNRESOLVED, lines, unresolved=1)); continue
        lines.append("package: %s%s" % (pkg["file"], " (v%s)" % pkg["version"] if pkg["version"] else " (version unknown - not checkable)"))
        lines.append("flashes at next reboot" if comp.get("reboot") else "flashes immediately (iLO resets itself)")

        def run(pkg=pkg, comp=comp):
            upload_package(ilo, pkg, comp["name"])
            st = wait_update_idle(ilo)
            print("    iLO update state: %s" % st)
            if comp.get("flash_last") or not comp.get("reboot"):
                ilo.reconnect()
        items.append(Item("firmware", comp["name"], CHANGE, lines, run, late=bool(comp.get("flash_last"))))
    return items


# --------------------------------------------------------------------------- output / execution
def show(items, verbose):
    for sec in ("firmware", "bios", "boot", "ilo"):
        part = [i for i in items if i.section == sec]
        if not part:
            continue
        print("\n=== %s %s" % (sec.upper(), "=" * (60 - len(sec))))
        for it in part:
            print(" [%s] %-11s %s" % (SYMBOL[it.status], it.status, it.title))
            for ln in it.lines:
                if verbose or it.status != OK:
                    print("        " + ln)


def execute(items):
    fails = 0
    ordered = [i for i in items if not i.late] + [i for i in items if i.late]
    for it in ordered:
        if it.status != CHANGE or not it.run:
            continue
        print(" -> applying: %s" % it.title)
        try:
            it.run()
            print("    done")
        except Exception as e:
            it.error = str(e)
            fails += 1
            print("    FAILED: %s" % e)
    return fails


def dump_bios(ilo, path):
    bios = ilo.get(ilo.resolve("bios"))
    reg = load_registry(ilo, bios)
    out = {"Attributes": bios.get("Attributes", {}),
           "Registry": {n: {"DisplayName": e.get("DisplayName"), "MenuPath": e.get("MenuPath"), "Type": e.get("Type"),
                            "ReadOnly": e.get("ReadOnly"),
                            "Values": [(v.get("ValueName"), v.get("ValueDisplayName")) for v in e.get("Value", [])]}
                        for n, e in reg.items()}}
    with open(path, "w") as f:
        json.dump(out, f, indent=1)
    print("wrote %s (%d attributes, %d in registry)" % (path, len(out["Attributes"]), len(reg)))


def reboot(ilo, reset_type):
    uri = dig(ilo.get(ilo.system(), fresh=True), "Actions", "#ComputerSystem.Reset", "target") \
        or ilo.system().rstrip("/") + "/Actions/ComputerSystem.Reset/"
    check(ilo.c.post(uri, {"ResetType": reset_type}), "reboot (%s)" % reset_type)


# --------------------------------------------------------------------------- main
def parse_args(argv=None):
    p = argparse.ArgumentParser(description="Preview / apply the CID settings on an HPE DL380 Gen11 (iLO 6).",
                                epilog="With no --apply nothing is changed. Env: ILO_PASSWORD, MHSADMIN_PASSWORD.")
    p.add_argument("--ilo", required=True, help="iLO address, e.g. 10.0.0.100 or https://ilo.example")
    p.add_argument("-u", "--user", required=True, help="iLO administrator account")
    p.add_argument("--config", default=os.path.join(HERE, "cid_dl380_gen11_llx.json"))
    p.add_argument("--firmware-dir", help="folder containing the baseline firmware packages (.fwpkg/.flash)")
    p.add_argument("--only", default="firmware,bios,boot,ilo", help="comma list of sections to run (default: all)")
    p.add_argument("--sku", choices=["LLA", "LLB", "LLC"], help="SKU - sets the SNMP System Role")
    p.add_argument("--apply", action="store_true", help="make the changes (default is preview only)")
    p.add_argument("--reboot", action="store_true", help="after --apply, restart the server so pending changes take effect")
    p.add_argument("--reboot-type", default="GracefulRestart", choices=["GracefulRestart", "ForceRestart", "PushPowerButton"],
                   help="use ForceRestart if no OS is running (default GracefulRestart)")
    p.add_argument("--security-state", action="store_true", help="also change iLO security state to HighSecurity (needs iLO reset)")
    p.add_argument("--ilo-static-ip"); p.add_argument("--ilo-netmask"); p.add_argument("--ilo-gateway")
    p.add_argument("--show-inventory", action="store_true", help="print the firmware inventory (to tune matching in the config)")
    p.add_argument("--dump-bios", metavar="FILE", help="write current BIOS attributes + registry to FILE and exit")
    p.add_argument("--get", metavar="URI", action="append", help="read-only: print a Redfish resource (repeatable), then exit")
    p.add_argument("-v", "--verbose", action="store_true", help="also list compliant items")
    a = p.parse_args(argv)
    if a.reboot and not a.apply:
        p.error("--reboot only makes sense together with --apply")
    return a


def make_connect(args, password):
    redfish = _import_redfish()
    url = args.ilo if args.ilo.startswith("http") else "https://" + args.ilo

    def connect():
        c = redfish.RedfishClient(base_url=url, username=args.user, password=password)
        c.login()
        return c
    return connect


def run(args, connect=None):
    cfg = json.load(open(args.config))
    if connect is None:
        pw = os.environ.get("ILO_PASSWORD") or getpass.getpass("iLO password for %s: " % args.user)
        connect = make_connect(args, pw)
    ilo = Ilo(connect)
    sections = {s.strip() for s in args.only.split(",")}
    params = {"SKU": args.sku}
    mode = "APPLY" if args.apply else "PREVIEW (nothing will be changed)"
    print("iLO %s  -  %s  -  CID rev 1.9  -  script v%s, config v%s" % (args.ilo, mode, VERSION, cfg.get("_version", "?")))
    fails, items = 0, []
    try:
        if args.get:
            for uri in args.get:
                r = ilo.c.get(uri)
                print("### GET %s -> HTTP %s" % (uri, r.status))
                print(json.dumps(r.dict, indent=1, sort_keys=True))
            return 0
        if args.dump_bios:
            dump_bios(ilo, args.dump_bios)
            return 0

        def settings_plan():
            out = []
            if "bios" in sections:
                out += plan_bios(ilo, cfg)
                out += [plan_generic(ilo, "bios", s, params) for s in cfg.get("system", [])]
            if "boot" in sections:
                out += plan_boot(ilo, cfg)
            if "ilo" in sections:
                out += plan_ilo(ilo, cfg, args, params)
            return out

        if "firmware" in sections:
            fw = plan_firmware(ilo, cfg, args)
            items += fw
            if args.apply:
                show(fw, args.verbose)
                fails += execute(fw)
                ilo.cache.clear()           # re-read everything: iLO/BIOS firmware may have changed names
        rest = settings_plan()
        items += rest
        if not args.apply or "firmware" not in sections:
            show(items if not args.apply else rest, args.verbose)
        else:
            show(rest, args.verbose)
        if args.apply:
            print("\n=== APPLYING ====================================================")
            fails += execute(rest)

        unresolved = sum(i.unresolved for i in items)
        counts = {s: sum(1 for i in items if i.status == s) for s in (OK, CHANGE, PENDING, UNRESOLVED, MANUAL, SKIP)}
        print("\nSummary: %s" % ", ".join("%d %s" % (n, s.lower()) for s, n in counts.items() if n))
        if unresolved:
            print("  %d setting(s) could not be mapped to this iLO/BIOS (marked '?') - fix names in the config JSON, or use --dump-bios." % unresolved)
        if "ilo" in sections or "bios" in sections:
            print("\nNot automated (check by hand / other tooling):")
            for m in cfg.get("manual_checklist", []):
                print("  - " + m)
        if not args.apply:
            n = counts[CHANGE]
            print("\nPreview only. %s Re-run with --apply to make these changes%s." % (
                "%d item(s) would change." % n if n else "Nothing to change.", " (add --reboot to restart afterwards)"))
        elif args.reboot:
            if fails:
                print("\n%d item(s) failed - NOT rebooting. Fix and re-run." % fails)
            else:
                print("\nRebooting (%s) so pending BIOS changes and queued firmware are applied ..." % args.reboot_type)
                reboot(ilo, args.reboot_type)
                print("Reboot requested. Firmware flashes during POST can take 10-30 minutes; re-run in preview mode afterwards to verify.")
        elif any(i.status in (CHANGE, PENDING) and i.section in ("bios", "boot", "firmware") for i in items):
            print("\nBIOS / boot order / queued firmware only take effect after a restart - run again with --apply --reboot, or restart manually.")
        return 1 if fails else (2 if (unresolved and not args.apply) else 0)
    finally:
        try:
            ilo.c.logout()
        except Exception:
            pass


if __name__ == "__main__":
    sys.exit(run(parse_args()))
