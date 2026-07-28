const fs = require('fs');
const path = require('path');
const https = require('https');

const BASE = 'https://api-mcp.51ifind.com:8643/ds-mcp-servers';
const SERVERS = {
  stock: `${BASE}/hexin-ifind-ds-stock-mcp`,
  fund: `${BASE}/hexin-ifind-ds-fund-mcp`,
  edb: `${BASE}/hexin-ifind-ds-edb-mcp`,
  news: `${BASE}/hexin-ifind-ds-news-mcp`,
  bond: `${BASE}/hexin-ifind-ds-bond-mcp`,
  global_stock: `${BASE}/hexin-ifind-ds-global-stock-mcp`,
  index: `${BASE}/hexin-ifind-ds-index-mcp`,
};
const BLOCKED_KEYS = new Set(['__proto__', 'prototype', 'constructor']);
const sessions = {};
const requestIds = {};
const toolSets = {};

function configCandidates() {
  const candidates = [];
  if (process.env.IFIND_MCP_CONFIG) {
    candidates.push(path.resolve(process.env.IFIND_MCP_CONFIG));
  }
  candidates.push(path.resolve(__dirname, '..', 'mcp_config.json'));
  candidates.push(path.resolve(__dirname, 'mcp_config.json'));
  return [...new Set(candidates)];
}

function loadAuth() {
  const fromEnv = String(process.env.IFIND_AUTH_TOKEN || '').trim();
  if (fromEnv) {
    return { token: fromEnv, source: 'IFIND_AUTH_TOKEN' };
  }
  for (const candidate of configCandidates()) {
    if (!fs.existsSync(candidate)) {
      continue;
    }
    let payload;
    try {
      payload = JSON.parse(fs.readFileSync(candidate, 'utf8'));
    } catch (error) {
      throw new Error(`invalid iFinD config JSON: ${candidate}`);
    }
    const token = String(payload.auth_token || '').trim();
    if (token) {
      return { token, source: candidate };
    }
  }
  return { token: '', source: '' };
}

function requireAuth() {
  const auth = loadAuth();
  if (!auth.token) {
    throw new Error(
      'iFinD is not configured. Set IFIND_AUTH_TOKEN or IFIND_MCP_CONFIG, ' +
      'or add mcp_config.json to the registered Skill directory. Do not search the filesystem.'
    );
  }
  return auth;
}

function nextId(serverType) {
  requestIds[serverType] = (requestIds[serverType] || 0) + 1;
  return requestIds[serverType];
}

function validateParams(params) {
  if (params === null || typeof params !== 'object' || Array.isArray(params)) {
    throw new TypeError('input must be a JSON object');
  }
  function walk(value) {
    if (value === null) return;
    if (Array.isArray(value)) {
      value.forEach(walk);
      return;
    }
    if (typeof value !== 'object') {
      if (typeof value === 'number' && !Number.isFinite(value)) {
        throw new TypeError('input contains invalid number');
      }
      if (['bigint', 'function', 'symbol', 'undefined'].includes(typeof value)) {
        throw new TypeError('input contains unsupported value type');
      }
      return;
    }
    for (const key of Object.keys(value)) {
      if (BLOCKED_KEYS.has(key)) {
        throw new TypeError('input contains blocked field');
      }
      walk(value[key]);
    }
  }
  walk(params);
}

function headers(serverType, token) {
  const result = {
    'Content-Type': 'application/json',
    Accept: 'application/json, text/event-stream',
    Authorization: token,
  };
  if (sessions[serverType]) {
    result['Mcp-Session-Id'] = sessions[serverType];
  }
  return result;
}

function post(serverType, payload, token, timeoutSeconds = 60) {
  return new Promise((resolve, reject) => {
    const url = new URL(SERVERS[serverType]);
    const request = https.request(
      {
        hostname: url.hostname,
        port: url.port,
        path: url.pathname,
        method: 'POST',
        headers: headers(serverType, token),
        timeout: timeoutSeconds * 1000,
      },
      (response) => {
        let body = '';
        response.on('data', (chunk) => { body += chunk; });
        response.on('end', () => {
          let data = null;
          if (body.trim()) {
            try {
              data = JSON.parse(body);
            } catch {
              data = body;
            }
          }
          resolve({ response, data });
        });
      }
    );
    request.on('error', reject);
    request.on('timeout', () => {
      request.destroy();
      reject(new Error(`request timeout after ${timeoutSeconds}s`));
    });
    request.write(JSON.stringify(payload));
    request.end();
  });
}

async function initialize(serverType, token) {
  if (sessions[serverType]) return;
  const { response, data } = await post(
    serverType,
    {
      jsonrpc: '2.0',
      id: nextId(serverType),
      method: 'initialize',
      params: {
        protocolVersion: '2025-03-26',
        capabilities: {},
        clientInfo: { name: 'nanobot-ifind-skill', version: '1.0.0' },
      },
    },
    token,
    30
  );
  if (response.statusCode >= 400) {
    throw new Error(`initialize HTTP error: ${response.statusCode}`);
  }
  if (data && typeof data === 'object' && data.error) {
    throw new Error(`initialize failed: ${JSON.stringify(data.error)}`);
  }
  const sessionId = response.headers['mcp-session-id'];
  if (!sessionId) {
    throw new Error('initialize succeeded without Mcp-Session-Id');
  }
  sessions[serverType] = sessionId;
  await post(
    serverType,
    { jsonrpc: '2.0', method: 'notifications/initialized' },
    token,
    10
  );
}

async function listTools(serverType) {
  if (!SERVERS[serverType]) {
    throw new Error(`unknown server_type: ${serverType}`);
  }
  const { token } = requireAuth();
  await initialize(serverType, token);
  const { response, data } = await post(
    serverType,
    {
      jsonrpc: '2.0',
      id: nextId(serverType),
      method: 'tools/list',
      params: {},
    },
    token
  );
  if (response.statusCode >= 400) {
    throw new Error(`tools/list HTTP error: ${response.statusCode}`);
  }
  if (data && typeof data === 'object' && data.error) {
    return { ok: false, status_code: response.statusCode, error: data.error };
  }
  return { ok: true, status_code: response.statusCode, data };
}

async function loadToolSet(serverType) {
  if (toolSets[serverType]) return toolSets[serverType];
  const response = await listTools(serverType);
  if (!response.ok) {
    throw new Error(`tools/list failed: ${JSON.stringify(response.error)}`);
  }
  const tools = response.data?.result?.tools;
  if (!Array.isArray(tools)) {
    throw new Error('invalid tools/list response');
  }
  toolSets[serverType] = new Set(
    tools.filter((tool) => tool && typeof tool.name === 'string').map((tool) => tool.name)
  );
  return toolSets[serverType];
}

async function call(serverType, toolName, params) {
  if (!SERVERS[serverType]) {
    throw new Error(`unknown server_type: ${serverType}`);
  }
  validateParams(params);
  const allowedTools = await loadToolSet(serverType);
  if (!allowedTools.has(toolName)) {
    throw new Error(`tool_name not allowed for ${serverType}: ${toolName}`);
  }
  const { token } = requireAuth();
  const { response, data } = await post(
    serverType,
    {
      jsonrpc: '2.0',
      id: nextId(serverType),
      method: 'tools/call',
      params: { name: toolName, arguments: params },
    },
    token
  );
  if (data && typeof data === 'object' && data.error) {
    return {
      ok: false,
      status_code: response.statusCode,
      error: data.error,
    };
  }
  if (response.statusCode >= 400) {
    throw new Error(`tools/call HTTP error: ${response.statusCode}`);
  }
  return { ok: true, status_code: response.statusCode, data };
}

function parseParams(raw) {
  let params;
  try {
    params = JSON.parse(raw);
  } catch (error) {
    throw new TypeError(`json_params must be valid JSON: ${error.message}`);
  }
  validateParams(params);
  return params;
}

function usage() {
  return [
    'Usage:',
    "  node scripts/call-node.js <server_type> <tool_name> '<json_params>'",
    '  node scripts/call-node.js list-tools <server_type>',
    '  node scripts/call-node.js --check',
  ].join('\n');
}

async function main(argv = process.argv.slice(2)) {
  if (argv.length === 0 || argv[0] === '--help' || argv[0] === '-h') {
    console.log(usage());
    return;
  }
  if (argv[0] === '--check') {
    const auth = requireAuth();
    console.log(JSON.stringify({ ok: true, configured: true, source: auth.source }));
    return;
  }
  let result;
  if (argv[0] === 'list-tools') {
    if (argv.length !== 2) {
      throw new TypeError(`list-tools requires one server_type\n\n${usage()}`);
    }
    result = await listTools(argv[1]);
  } else {
    if (argv.length !== 3) {
      throw new TypeError(`call requires server_type, tool_name and json_params\n\n${usage()}`);
    }
    result = await call(argv[0], argv[1], parseParams(argv[2]));
  }
  console.log(JSON.stringify(result, null, 2));
  if (!result.ok) process.exitCode = 2;
}

module.exports = {
  call,
  listTools,
  loadAuth,
  main,
  parseParams,
  requireAuth,
  usage,
};

if (require.main === module) {
  main().catch((error) => {
    console.error(`iFinD call failed: ${error.message}`);
    process.exitCode = 1;
  });
}
