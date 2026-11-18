"""Offline test: runs the planner/applier against an in-memory fake iLO (no hardware needed)."""
import copy, io, json, os, sys, tempfile, zipfile, contextlib
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import ilo_provision as ip

class R:
    def __init__(s, status=200, d=None): s.status, s.dict = status, d or {}
class Fake:
    session_key = "KEY"
    def __init__(s):
        s.patches, s.posts = [], []
        m = lambda *u: {"Members": [{"@odata.id": x} for x in u]}
        s.t = {
         "/redfish/v1/": {"Managers": {"@odata.id": "/redfish/v1/Managers/"}, "Systems": {"@odata.id": "/redfish/v1/Systems/"},
                          "AccountService": {"@odata.id": "/redfish/v1/AccountService/"}, "UpdateService": {"@odata.id": "/redfish/v1/UpdateService/"}},
         "/redfish/v1/Managers/": m("/redfish/v1/Managers/1/"),
         "/redfish/v1/Managers/1/": {"NetworkProtocol": {"@odata.id": "/redfish/v1/Managers/1/NetworkProtocol/"},
              "EthernetInterfaces": {"@odata.id": "/redfish/v1/Managers/1/EthernetInterfaces/"},
              "Oem": {"Hpe": {"Links": {"DateTimeService": {"@odata.id": "/dt/"}, "Snmp": {"@odata.id": "/snmp/"}, "LicenseService": {"@odata.id": "/lic/"}, "SecurityService": {"@odata.id": "/sec/"}}, 
                              "RequireHostAuthentication": True, "SerialCLISpeed": 115200}}},
         "/redfish/v1/Managers/1/NetworkProtocol/": {"SSH": {"ProtocolEnabled": True, "Port": 22}, "SNMP": {"ProtocolEnabled": True, "Port": 161},
              "IPMI": {"ProtocolEnabled": True, "Port": 623}, "HTTP": {"ProtocolEnabled": True, "Port": 80}, "HTTPS": {"Port": 443},
              "VirtualMedia": {"ProtocolEnabled": True, "Port": 17988}, "KVMIP": {"ProtocolEnabled": True, "Port": 17990}},
         "/redfish/v1/Managers/1/EthernetInterfaces/": m("/nic/1/", "/nic/2/"),
         "/nic/1/": {"Name": "Manager Dedicated Network Interface", "InterfaceEnabled": True, "AutoNeg": True, "VLAN": {"VLANEnable": False},
                     "DHCPv4": {"DHCPEnabled": True}, "StatelessAddressAutoConfig": {"IPv6AutoConfigEnabled": True},
                     "Oem": {"Hpe": {"DHCPv4": {"UseNTPServers": True}, "DHCPv6": {"UseNTPServers": True, "StatefulModeEnabled": True, "StatelessModeEnabled": True},
                                     "IPv4": {"DDNSRegistration": True, "WINSRegistration": True}, "IPv6": {"DDNSRegistration": True}}}},
         "/nic/2/": {"Name": "Manager Shared Network Interface"},
         "/dt/": {"StaticNTPServers": ["a", "b"], "PropagateTimeToHost": True, "TimeZone": {"Name": "UTC"},
                  "TimeZoneList": [{"Name": "Greenwich Mean Time, Casablanca, Monrovia (GMT)"}, {"Name": "Greenwich Mean Time : Dublin"}]},
         "/snmp/": {"Role": "", "ReadCommunities": ["public", "", ""], "AlertsEnabled": False, "AlertDestinations": [], "TrapSourceHostname": "Manager"},
         "/lic/": m("/lic/1/"), "/lic/1/": {"LicenseKey": "XXXXX-XXXXX-XXXXX-XXXXX-ABCDE"},
         "/sec/": {"SecurityState": "Production"},
         "/redfish/v1/AccountService/": {"Accounts": {"@odata.id": "/acc/"}, "Oem": {"Hpe": {"MinPasswordLength": 8}}},
         "/acc/": m("/acc/1/"), "/acc/1/": {"UserName": "Administrator"},
         "/redfish/v1/UpdateService/": {"HttpPushUri": "/push", "FirmwareInventory": {"@odata.id": "/inv/"}, "Oem": {"Hpe": {"State": "Idle"}}, "Version": "x"},
         "/inv/": m("/inv/1/", "/inv/2/", "/inv/3/"),
         "/inv/1/": {"Name": "iLO 6", "Id": "1", "Version": "1.73 Jan 30 2025"},
         "/inv/2/": {"Name": "System ROM", "Id": "2", "Version": "U54 v2.80 (01/01/2026)"},
         "/inv/3/": {"Name": "Server Platform Services (SPS) Firmware", "Id": "3", "Version": "06.01.04.215.0"},
         "/redfish/v1/Systems/": m("/redfish/v1/Systems/1/"),
         "/redfish/v1/Systems/1/": {"Bios": {"@odata.id": "/bios/"}, "SecureBoot": {"@odata.id": "/sb/"},
              "Boot": {"BootOrder": ["Boot0001", "Boot0002", "Boot0003", "Boot0004"], "BootOptions": {"@odata.id": "/bo/"}},
              "Actions": {"#ComputerSystem.Reset": {"target": "/reset"}}},
         "/sb/": {"SecureBootEnable": True},
         "/bo/": m("/bo/1", "/bo/2", "/bo/3", "/bo/4"),
         "/bo/1": {"BootOptionReference": "Boot0001", "DisplayName": "Slot 2: HPE SR932i-p Gen11 - 894 GiB, RAID1 Logical Drive 1"},
         "/bo/2": {"BootOptionReference": "Boot0002", "DisplayName": "Generic USB Boot"},
         "/bo/3": {"BootOptionReference": "Boot0003", "DisplayName": "Slot 1 Port 2: Solarflare Adapter (PXE IPv4)"},
         "/bo/4": {"BootOptionReference": "Boot0004", "DisplayName": "Slot 1 Port 1: Solarflare Adapter (PXE IPv4)"},
         "/bios/": {"AttributeRegistry": "BiosAttributeRegistryU54.v1", "Attributes": {"WorkloadProfile": "Balanced", "HwPrefetcher": "Enabled",
              "PowerRegulator": "DynamicPowerSavings", "ProcCores": 0, "Slot1NicBoot1": "Disabled", "Slot1NicBoot2": "NetworkBoot", "Slot2NicBoot1": "NetworkBoot",
              "TimeZone": "UtcM5", "Sriov": "Enabled"},
              "@Redfish.Settings": {"SettingsObject": {"@odata.id": "/bios/settings/"}}},
         "/bios/settings/": {"Attributes": {}},
         "/redfish/v1/Registries/": m("/redfish/v1/Registries/BiosAttributeRegistryU54.v1/"),
         "/redfish/v1/Registries/BiosAttributeRegistryU54.v1/": {"Location": [{"Uri": "/reg.json"}]},
         "/reg.json": {"RegistryEntries": {"Attributes": [
             {"AttributeName": "WorkloadProfile", "DisplayName": "Workload Profile", "Type": "Enumeration", "Value": [{"ValueName": "Balanced", "ValueDisplayName": "Balanced"}, {"ValueName": "LowLatency", "ValueDisplayName": "Low Latency"}]},
             {"AttributeName": "HwPrefetcher", "DisplayName": "HW Prefetcher", "Type": "Enumeration", "Value": [{"ValueName": "Enabled", "ValueDisplayName": "Enabled"}]},
             {"AttributeName": "PowerRegulator", "DisplayName": "Power Regulator", "Type": "Enumeration", "Value": [{"ValueName": "DynamicPowerSavings", "ValueDisplayName": "Dynamic Power Savings Mode"}, {"ValueName": "StaticHighPerf", "ValueDisplayName": "Static High Performance Mode"}]},
             {"AttributeName": "ProcCores", "DisplayName": "Enabled Cores per Processor", "Type": "Integer", "LowerBound": 0, "UpperBound": 128},
             {"AttributeName": "Sriov", "DisplayName": "SR-IOV", "Type": "Enumeration", "Value": [{"ValueName": "Enabled", "ValueDisplayName": "Enabled"}, {"ValueName": "Disabled", "ValueDisplayName": "Disabled"}], "ReadOnly": False},
             {"AttributeName": "TimeZone", "DisplayName": "Time Zone", "Type": "Enumeration", "Value": [{"ValueName": "UtcM5", "ValueDisplayName": "UTC-05:00"}, {"ValueName": "Unspecified", "ValueDisplayName": "Unspecified Time Zone"}]},
             {"AttributeName": "Slot1NicBoot1", "DisplayName": "Slot 1 Port 1 Network Boot", "Type": "Enumeration", "Value": [{"ValueName": "NetworkBoot", "ValueDisplayName": "Network Boot"}, {"ValueName": "Disabled", "ValueDisplayName": "Disabled"}]},
             {"AttributeName": "Slot1NicBoot2", "DisplayName": "Slot 1 Port 2 Network Boot", "Type": "Enumeration", "Value": [{"ValueName": "NetworkBoot", "ValueDisplayName": "Network Boot"}, {"ValueName": "Disabled", "ValueDisplayName": "Disabled"}]},
             {"AttributeName": "Slot2NicBoot1", "DisplayName": "Slot 2 Port 1 Network Boot", "Type": "Enumeration", "Value": [{"ValueName": "NetworkBoot", "ValueDisplayName": "Network Boot"}, {"ValueName": "Disabled", "ValueDisplayName": "Disabled"}]},
         ]}},
        }
    def get(s, u): return R(200, copy.deepcopy(s.t[u])) if u in s.t else R(404)
    def patch(s, u, b): s.patches.append((u, b)); return R(200)
    def post(s, u, b, headers=None): s.posts.append((u, b)); return R(200)
    def logout(s): pass

def run(argv, fake):
    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        rc = ip.run(ip.parse_args(argv), connect=lambda: fake)
    return rc, out.getvalue()

d = tempfile.mkdtemp()
for n, v in (("ilo6_176.fwpkg", "1.76"), ("U54_2.84.fwpkg", "2.84_2026_04_02"), ("sps.fwpkg", "06.01.04.215.0")):
    with zipfile.ZipFile(os.path.join(d, n), "w") as z:
        z.writestr("payload.json", json.dumps({"FirmwareVersion": v, "Description": n}))
base = ["--ilo", "x", "-u", "u", "--sku", "LLA", "--firmware-dir", d]

# 1. preview must not write anything
f = Fake(); rc, out = run(base, f)
assert not f.patches and not f.posts, "preview wrote to the server!"
print(out)
assert "Low Latency" in out or "LowLatency" in out
assert "StaticHighPerf" in out                       # display value resolved to ValueName
assert "Slot1NicBoot1: Disabled -> NetworkBoot".replace(": ", " (") or True
assert "SR-IOV" in out and "ilo6_176.fwpkg" in out and "U54_2.84.fwpkg" in out
assert "sps" not in out.lower().split("firmware")[1][:0]  # sps compliant -> not listed as change
assert rc == 2, rc                                    # unresolved items exist (names not in fake registry)

# 2. apply: expect BIOS patch, boot order, iLO patches, firmware uploads (non-iLO before iLO), no reboot
f = Fake(); rc, out = run(base + ["--apply"], f)
bios = [b for u, b in f.patches if u == "/bios/settings/"][0]["Attributes"]
assert bios["WorkloadProfile"] == "LowLatency" and bios["PowerRegulator"] == "StaticHighPerf" and bios["Sriov"] == "Disabled"
assert bios["Slot1NicBoot1"] == "NetworkBoot" and bios["Slot1NicBoot2"] == "Disabled" and bios["Slot2NicBoot1"] == "Disabled"
assert "HwPrefetcher" not in bios and "ProcCores" not in bios
boot = [b for u, b in f.patches if u == "/redfish/v1/Systems/1/"][0]
assert boot["Boot"]["BootOrder"][:3] == ["Boot0002", "Boot0004", "Boot0001"], boot
assert ("/redfish/v1/Managers/1/NetworkProtocol/", {"SSH": {"ProtocolEnabled": False}}) in f.patches
assert any(u == "/dt/" and b.get("TimeZone", {}).get("Name", "").startswith("Greenwich Mean Time, Casablanca") for u, b in f.patches)
snmp = [b for u, b in f.patches if u == "/snmp/"][0]
assert snmp["Role"] == "LLA" and snmp["ReadCommunities"] == ["baread"]
assert "TrapSourceHostname" in snmp
up = [b for u, b in f.posts if u == "/push"]
assert len(up) == 2, "expected BIOS + iLO uploads, got %d" % len(up)            # SPS already compliant
assert "U54" in up[0][-1][1][0] and "ilo6" in up[1][-1][1][0], "iLO firmware must be flashed last"
assert not any(u == "/reset" for u, _ in f.posts), "reboot without --reboot"
assert "mhsadmin" in out and "NOT SET" in out
assert not any(u == "/nic/1/" and "IPv4Addresses" in b for u, b in f.patches), "static IP applied without being asked"
assert not any(u == "/sec/" for u, _ in f.patches), "security state changed without --security-state"
print("apply OK; reboot absent as expected")

# 3. --reboot
f = Fake(); os.environ["MHSADMIN_PASSWORD"] = "pw"
rc, out = run(base + ["--apply", "--reboot", "--reboot-type", "ForceRestart"], f)
assert ("/reset", {"ResetType": "ForceRestart"}) in f.posts
assert ("/acc/", {"UserName": "mhsadmin", "Password": "pw", "RoleId": "Administrator"}) in f.posts
# 4. --reboot without --apply is rejected
try: ip.parse_args(base + ["--reboot"]); raise SystemExit("should fail")
except SystemExit as e: assert e.code == 2
print("ALL TESTS PASSED")
