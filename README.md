# HotWater

HotWater is a local dashboard for an A. O. Smith iCOMM water heater. See 105-degree-F mixed-water reserve, recent draw and recovery, seven days of daily statistics, and a rolling 24-hour chart. The separate authenticated admin page supports setpoint, Vacation and Standard commands, diagnostics, schedules, and temperature presets. The read-only Hub view is at /hub.

See [SETUP.md](SETUP.md) for the Windows and OpenWrt setup. The LAN addresses, example MAC addresses, Duck DNS names, and Hub name are examples. Set the addresses consistently in the gauge, admin and Caddy configuration before deploying. The optional Hub launcher needs its device name, Node dependencies and CATT runtime set for your installation.

The relay runtime is bundled with its [upstream license](relay-src/LICENSE) and [revision information](relay-src/UPSTREAM.md).

For the Windows startup scripts, create the prerequisite virtual environment with `py -3.13 -m venv .venv` and install the pinned relay dependencies from `relay-src/relay/requirements.txt`. The optional Hub launcher was used with Node.js 24.18.0, `@matter/main@0.18.0`, and CATT 0.13.3. Its dependency folders are `C:\HotWaterRuntime\hub-launcher\node_modules` and `C:\HotWaterRuntime\hub-launcher\cast-packages`.
