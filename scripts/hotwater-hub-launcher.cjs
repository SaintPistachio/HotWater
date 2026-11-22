"use strict";

const { execFile } = require("node:child_process");
const { appendFileSync, mkdirSync, rmSync, writeFileSync } = require("node:fs");
const { createRequire } = require("node:module");
const { networkInterfaces } = require("node:os");
const { join } = require("node:path");
const { promisify } = require("node:util");

const runtime = "C:\\HotWaterRuntime\\hub-launcher";
const state = join(runtime, "matter-state");
const pairingFile = join(state, "pairing-code.txt");
const logFile = "C:\\HotWaterRuntime\\logs\\hub-launcher.log";
const gaugeUrl = "http://192.168.50.10:8125/hub";
const hubName = "Living Room display";
const label = "Show Hot Water";
const runFile = promisify(execFile);

function log(message) {
  appendFileSync(logFile, `${new Date().toISOString()} ${message}\n`);
}

async function castGauge() {
  const response = await fetch(gaugeUrl, { signal: AbortSignal.timeout(4000) });
  if (!response.ok || !(await response.text()).includes('class="hub-view"')) {
    throw new Error("The read-only HotWater Hub page did not respond.");
  }

  const python = join(__dirname, "..", ".runtime", "python313", "python.exe");
  const packages = join(runtime, "cast-packages");
  const entry = "import runpy, sys; sys.path.insert(0, sys.argv.pop(1)); runpy.run_module('catt.cli', run_name='__main__')";
  await runFile(python, ["-B", "-c", entry, packages, "-d", hubName, "cast_site", gaugeUrl], {
    timeout: 20000,
    windowsHide: true,
  });
  log(`Sent the HotWater Hub page to ${hubName}.`);
}

async function main() {
  mkdirSync(state, { recursive: true });
  const local = networkInterfaces().Ethernet ?? [];
  if (!local.some(address => address.address === "192.168.50.10") ||
      !local.some(address => address.family === "IPv6")) {
    throw new Error("Ethernet must have the HotWater LAN IPv4 address and an IPv6 address.");
  }

  const requireMatter = createRequire(join(runtime, "package.json"));
  const { Endpoint, Environment, Logger, LogLevel, ServerNode, VendorId } = requireMatter("@matter/main");
  const { OnOffPlugInUnitDevice } = requireMatter("@matter/main/devices/on-off-plug-in-unit");
  const environment = Environment.default;
  environment.vars.set("storage.path", state);
  environment.vars.set("mdns.networkinterface", "Ethernet");
  Logger.level = LogLevel.WARN; // Pairing credentials belong in the restricted file only.

  const server = await ServerNode.create({
    id: "hotwater-hub-launcher",
    network: { port: 5540, listeningAddressIpv4: "192.168.50.10" },
    productDescription: { name: label, deviceType: OnOffPlugInUnitDevice.deviceType },
    basicInformation: {
      vendorName: "HotWater",
      vendorId: VendorId(0xfff1),
      productName: label,
      productId: 0x8000,
      nodeLabel: label,
      serialNumber: "hotwater-hub-launcher",
    },
  });
  const outlet = new Endpoint(OnOffPlugInUnitDevice, { id: "show-hot-water" });
  await server.add(outlet);
  await outlet.set({ onOff: { onOff: false } });

  let casting = false;
  outlet.events.onOff.onOff$Changed.on(on => {
    if (!on || casting) return;
    casting = true;
    setImmediate(async () => {
      try {
        log(`Received ${label} tap.`);
        await castGauge();
      } catch (error) {
        log(`Cast failed: ${error.stderr?.trim() || error.message}`);
      } finally {
        try {
          await outlet.set({ onOff: { onOff: false } });
        } catch (error) {
          log(`Could not reset the tile: ${error.message}`);
        } finally {
          casting = false;
        }
      }
    });
  });

  const updatePairingFile = () => {
    if (server.lifecycle.isCommissioned) {
      rmSync(pairingFile, { force: true });
      log(`${label} is paired with a Matter controller.`);
    } else {
      writeFileSync(pairingFile, `${server.state.commissioning.pairingCodes.manualPairingCode}\n`, { mode: 0o600 });
      log(`${label} is ready to pair.`);
    }
  };
  server.lifecycle.commissioned.on(updatePairingFile);
  server.lifecycle.decommissioned.on(updatePairingFile);
  await server.start();
  updatePairingFile();
  log(`HotWater Hub launcher is listening on Ethernet UDP 5540 for ${hubName}.`);
}

main().catch(error => {
  log(`Hub launcher could not start: ${error.message}`);
  console.error(error.message);
  process.exit(1);
});
