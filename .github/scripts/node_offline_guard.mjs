// Node-level egress guard for local frontend tests/builds, not an OS sandbox.
import net from 'node:net';
import tls from 'node:tls';
import dgram from 'node:dgram';
import dns from 'node:dns';
import http from 'node:http';
import https from 'node:https';
import { syncBuiltinESMExports } from 'node:module';
let attempts = 0;
const blocked = (kind) => () => {
  attempts += 1;
  process.stderr.write(`OFFLINE_GUARD_BLOCKED ${kind}\n`);
  throw new Error(`Offline frontend guard blocked ${kind}`);
};
process.on('exit', () => { if (attempts) process.exitCode = 1; });
net.Socket.prototype.connect = blocked('socket.connect');
net.connect = net.createConnection = blocked('net.connect');
tls.connect = blocked('tls.connect');
dgram.createSocket = blocked('dgram.createSocket');
for (const key of Object.keys(dns)) {
  if ((key === 'lookup' || key.startsWith('resolve') || key === 'reverse') && typeof dns[key] === 'function') {
    dns[key] = blocked(`dns.${key}`);
  }
}
for (const key of Object.keys(dns.promises)) {
  if ((key === 'lookup' || key.startsWith('resolve') || key === 'reverse') && typeof dns.promises[key] === 'function') {
    dns.promises[key] = blocked(`dns.promises.${key}`);
  }
}
for (const resolver of [dns.Resolver?.prototype, dns.promises.Resolver?.prototype]) {
  for (const key of Object.getOwnPropertyNames(resolver || {})) {
    if ((key.startsWith('resolve') || key === 'reverse') && typeof resolver[key] === 'function') {
      resolver[key] = blocked(`dns.Resolver.${key}`);
    }
  }
}
http.request = http.get = blocked('http');
https.request = https.get = blocked('https');
globalThis.fetch = blocked('fetch');
syncBuiltinESMExports();
