# Local hot water reader: setup

Layout: OpenWrt router 192.168.50.1 (LAN) / 192.168.51.2 (WAN), office PC 192.168.50.10, water heater 192.168.50.20. The relay and gauge run on the office PC in C:\HotWater.

How it works: the router sends only the heater's HTTPS traffic (TCP 443) to the relay on the PC. The relay passes it on to Ayla's cloud unchanged, reads the heater's readings on the way, and switches on the heater's local mode. The gauge page reads the relay and shows the level.

## Run it

Use the Windows startup tasks for daily operation, or double-click `start-hotwater.cmd` to run the relay and gauge in two windows. The office PC must be powered on and awake. If the relay is down while the router redirect is active, the heater loses access to its cloud. See [Undo](#undo) if you plan to leave the relay off.

## Start with Windows

On the office PC, prepare the project-local Python runtime in a regular PowerShell 7 window. The setup verifies the Python archive checksum and installs the relay's pinned dependencies.

```powershell
Set-Location -LiteralPath 'C:\HotWater'
.\scripts\Prepare-HotWaterRuntime.ps1
```

Then open PowerShell 7 as administrator and register the two tasks under Windows Local Service.

```powershell
Set-Location -LiteralPath 'C:\HotWater'
.\scripts\Install-HotWaterStartup.ps1 -WhatIf
.\scripts\Install-HotWaterStartup.ps1
```

Local Service runs before sign-in and uses no personal Windows password. The installer adds read access for that identity to the HotWater runtime, code, and relay certificate files, and modify access to HotWater's `data` and log directories. Registration takes effect on the next boot or when you start the tasks explicitly.

### Check the tasks and switch over

1. In Task Scheduler Library, open `HotWater-Relay` and `HotWater-Gauge`. Confirm their account is **LOCAL SERVICE**, with **Run with highest privileges** cleared. Confirm the **At startup** trigger has a 30-second delay and repeats every minute indefinitely. Check that each action calls `scripts\run-hotwater-task.cmd` from the project folder with its corresponding `relay` or `gauge` argument.
2. Leave the relay running until the tasks are registered. When ready for a brief interruption, press Ctrl+C in both manual windows. From the elevated PowerShell window, run the following commands.

   ```powershell
   Start-ScheduledTask -TaskName 'HotWater-Relay'
   Start-ScheduledTask -TaskName 'HotWater-Gauge'
   Get-ScheduledTask -TaskName 'HotWater-Relay','HotWater-Gauge' | Select-Object TaskName, State
   Invoke-RestMethod 'http://127.0.0.1:8124/health' | Select-Object ok, uptime
   (Invoke-RestMethod 'http://127.0.0.1:8124/state').lan | Select-Object registered
   Invoke-RestMethod 'http://127.0.0.1:8125/api' | Select-Object percent, age_s, online, error
   ```

3. Both tasks should show `Running` and relay health should show `ok: true`. Once the heater has checked in, confirm `registered` is `True` and the gauge API reports a percentage and a numeric reading age. An idle heater can take time to send a reading. Check `relay.log` and `gauge.log` under `C:\HotWaterRuntime\logs` if either task exits or the endpoints fail.
4. After an approved reboot, leave Windows at the sign-in screen. Open the Android shortcut below and confirm it shows a heater reading. To test recovery after that boot, end only `HotWater-Gauge` in Task Scheduler, then confirm it returns to `Running` and the gauge reloads after the next one-minute trigger.

### Return to manual startup

In Task Scheduler, disable both `HotWater-Relay` and `HotWater-Gauge`, then end their running instances. When the task processes have exited and released their ports, double-click `start-hotwater.cmd` and verify the gauge loads. To switch back, re-enable both tasks and repeat the cutover above.

## Android home-screen shortcut

1. Connect the phone to home Wi-Fi and open [HotWater](http://192.168.50.10:8125/) in Chrome.
2. Tap **More > Install and create shortcut > Create shortcut**, name it **Hot Water**, then tap **Add**. [Google Chrome shortcut instructions](https://support.google.com/chrome/answer/15085120?co=GENIE.Platform%3DAndroid&hl=en)
3. Open the shortcut and confirm it shows the estimated gallons left at 105 °F, tank temperatures, time to recover, and reading age. The page checks for updates every 15 seconds while open. The age reports when the heater last sent telemetry.

## Hot water estimates and history

- Gallons left are estimated mixed water at 105 °F from the top and bottom tank temperatures and the heater's reported heat deficit. This uses 45 gallons of rated storage and assumes 55 °F incoming water. The whole-house mixing valve is set to 120 °F and the shower can blend that supply with more cold water. The gauge does not measure the outlet temperature. `TANK_GALLONS` and `INLET_F` let you adjust the tank size and inlet temperature assumptions.
- Current hot draw estimates combined household flow at 105 °F over a recent period of tank cooling, accounting for power the heater reported using. When the readings support a countdown, time left at the current pace becomes the most prominent reading. Otherwise fresh readings show the estimated shower time at a reference flow of 2 gal/min next to the available gallons. Stale readings leave both estimates blank.
- Time to 100% estimates when the heater will reach its active target once demand stops. It initially uses the 5,500 W rating (`RATED_WATTS`) and uses the median of qualifying recovery intervals from the past week after observing at least three. Actual refill time varies with continued use and the heater's cycling.
- The gauge checks the relay every 15 seconds even when the phone page is closed. Each fresh telemetry timestamp is saved once in `data\history.sqlite3` (`HOTWATER_HISTORY_DB` overrides the location). Charge and reserve history exclude targets below 105 °F, including Vacation, and isolated drops followed by a rebound faster than the heater can produce. An unusually sharp drop waits for another reading before it can count. The rolling 24-hour chart shows the lowest accepted charge in each five-minute interval. Daily stats run midnight to midnight in the office PC's time zone, with today's readings shown so far. Daily kWh comes from the heater's energy counter over observed intervals and may be partial. Blank rows indicate missing data. The heater does not report fixture-level flow.
- **Ready for another shower** starts with a 16-gallon planning reference at 105 °F. After enough complete lighter draws, it uses their typical inferred volume. Daily draw history groups draws by their start date. Draw comparisons and recovery trends show a learning message until enough qualifying readings are available.

## Owner-only HTTPS controls

The family gauge on port 8125 remains read-only. The optional admin page runs at `https://192.168.50.10:8443/` on the home LAN. The admin backend and relay command API bind to loopback. Caddy serves the admin page using a project-specific private certificate authority. The installer leaves Windows and phone trust stores unchanged.

1. From PowerShell 7 on the office PC run `./scripts/Install-HotWaterAdmin.ps1 -Stage Prepare`. It verifies the pinned Caddy archive, copies the admin runtime into the restricted `data\admin` directory, and generates the private CA without starting a listener. It exports `data\HotWater-CA.cer` and prints the certificate's SHA-256 fingerprint. Transfer only that public file to an approved admin device. On Android, open **Install a certificate** in Settings, choose **CA certificate**, and select `HotWater-CA.cer`. Compare the fingerprint if the phone shows it.
2. Set the owner password in a local terminal with `& .\.runtime\python313\python.exe -B .\data\admin\runtime\admin.py --set-password`. Enter it in the prompt. The program saves a salted hash in the restricted directory.
3. From elevated PowerShell run `./scripts/Install-HotWaterAdmin.ps1 -Stage Activate`. It registers and starts the admin and HTTPS tasks, verifies the sign-in page with the private CA, and allows TCP 8443 only from `192.168.50.0/24`. Confirm the browser accepts the HTTPS connection and that sign-in and sign-out work.
4. The admin page starts with heater controls locked. Reload the updated `HotWater-Relay` task in an agreed maintenance window and confirm `/state` includes `admin_control` and the local session is verified. An implementation operator must verify a small local temperature change and restoration on the actual heater before setting `temperature` to `true` in the restricted `data\admin\verified.json` file. For Vacation and Standard, record the verified Standard write and readback pairs (four hexadecimal digits each) in the relay task's `HOTWATER_STANDARD_WRITE_PAIR` and `HOTWATER_STANDARD_READ_PAIR` environment variables. Verify both mode transitions and the restored target before setting `modes` to `true`. The flags are separate, for example `{"temperature":true,"modes":false}`. The admin page reads the flags without a restart.
5. Keep credentials, the proxy key, the command audit, and all private certificate keys in `data\admin`. To rotate the password, repeat step 2 and restart `HotWater-Admin`. To take the admin page offline, stop and disable `HotWater-Https` and `HotWater-Admin` and disable the `HotWater-Admin-Https` firewall rule. Leave `HotWater-Gauge` and `HotWater-Relay` running.

Admin commands expire after a three-minute delivery window and use only the heater's local connection. Cancel works only before delivery. The administrator can use the command history to distinguish a confirmed result from an uncertain one.

The signed-in Admin page shows heater diagnostics, recent fault codes, and when it first observed Vacation or a return to Standard. Temperature presets save a tank target for manual confirmation. A scheduled mode change runs once at the selected time, shown in the browser's time zone. The office PC must be running. If the Admin service cannot submit the change within three minutes of the scheduled time, it marks the schedule as missed. Cancel a schedule before submission, or use the command history to cancel a queued command before delivery. Changing the Admin password invalidates pending schedules.

### Browser-trusted LAN addresses

- Family stats: `https://stats.example-hotwater.duckdns.org:8443/`
- Password-protected admin: `https://admin.example-hotwater.duckdns.org:8443/`

Both addresses use a single Let's Encrypt wildcard certificate. Caddy renews it through a Duck DNS TXT record using the token in the restricted `data\admin` folder. The public Duck DNS A record and the two OpenWrt local DNS records point to `192.168.50.10`. The HTTPS listener and firewall use the existing LAN scope on port 8443. The earlier IP-based addresses remain available.

For a replacement Windows runtime, prepare the existing admin setup first. Build Caddy v2.11.9 with `github.com/caddy-dns/duckdns@v0.5.0` using xcaddy. Run `./scripts/Set-HotWaterDuckDnsToken.ps1 -Subdomain example-hotwater` in an interactive PowerShell 7 window and enter the token at the hidden prompt. Then run `./scripts/Enable-HotWaterDuckDns.ps1 -Subdomain example-hotwater -CaddyBinary <path-to-caddy.exe>` from elevated PowerShell 7. Verify both HTTPS URLs from the LAN and confirm that the admin URL prompts for a password. The HTTPS task logs issuance and renewal in `C:\HotWaterRuntime\logs\admin-Https.log`.

## 1. Reserve addresses (OpenWrt, Network > DHCP and DNS > Static Leases)
- heater 02:00:00:00:00:20 -> 192.168.50.20
- office PC 02:00:00:00:00:10 -> 192.168.50.10

## 2. Windows firewall (admin PowerShell)
New-NetFirewallRule -DisplayName "iCOMM relay" -Direction Inbound -Protocol TCP -LocalPort 8444,8888 -RemoteAddress 192.168.50.1,192.168.50.20 -Action Allow
New-NetFirewallRule -DisplayName "Hot water gauge" -Direction Inbound -Protocol TCP -LocalPort 8125 -RemoteAddress LocalSubnet -Action Allow

## 3. Router redirect (OpenWrt over SSH: ssh root@192.168.50.1)
uci add firewall redirect
uci set firewall.@redirect[-1].name='icomm-relay'
uci set firewall.@redirect[-1].src='lan'
uci set firewall.@redirect[-1].src_ip='192.168.50.20'
uci set firewall.@redirect[-1].src_dport='443'
uci set firewall.@redirect[-1].proto='tcp'
uci set firewall.@redirect[-1].dest='lan'
uci set firewall.@redirect[-1].dest_ip='192.168.50.10'
uci set firewall.@redirect[-1].dest_port='8444'
uci set firewall.@redirect[-1].target='DNAT'
uci add firewall nat
uci set firewall.@nat[-1].name='icomm-relay-hairpin'
uci set firewall.@nat[-1].src='lan'
uci set firewall.@nat[-1].src_ip='192.168.50.20'
uci set firewall.@nat[-1].dest_ip='192.168.50.10'
uci set firewall.@nat[-1].dest_port='8444'
uci set firewall.@nat[-1].proto='tcp'
uci set firewall.@nat[-1].target='SNAT'
uci set firewall.@nat[-1].snat_ip='192.168.50.1'
uci commit firewall
/etc/init.d/firewall restart
Check: nft list ruleset | grep icomm (packet counters should rise).

## 4. Arm local mode
Heater breaker off 30 s, then on. Relay window should log "DSN rewrite: flipped lan-enabled..." and "LAN mode: registered with the module".

## Undo
In OpenWrt web page: Network > Firewall, delete the Port Forward "icomm-relay" and the NAT rule "icomm-relay-hairpin", Save and Apply. The heater goes straight back to its cloud.

## Notes
- The bundled [relay runtime](relay-src/UPSTREAM.md) is based on ha-icomm-local commit 2507eb2 with local HotWater command changes.
- Gauge percent = 1 - heat_deficit_wh / (45 gal x 8.34 x (setpoint - 55 F) / 3.412). Compare with real use for a few days.
- certs\ and data\heaters\ hold private keys: don't share or upload them.
