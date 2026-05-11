#!/usr/bin/env node
/**
 * Generate human-readable HTML reference showing all contract scenarios.
 * Usage: source .agent-config && node scripts/generate_contract_html.js > 0-MD/0-Documentation/public/AGENT-CONTRACT.html
 */
const https = require('https');
process.env.NODE_TLS_REJECT_UNAUTHORIZED = '0';

const api = process.env.VIBEFORGE_API;
const token = process.env.VIBEFORGE_TOKEN;
if (!api || !token) { console.error('Source .agent-config first'); process.exit(1); }

const baseUrl = api.replace('/api/v2', '');

// Fetch all three scenarios in parallel
Promise.all([
  fetchJson(baseUrl + '/agentnotes', null),           // unauthenticated
  fetchJson(baseUrl + '/agentnotes', token),           // authenticated, no project
  fetchJson(baseUrl + '/agentnotes/vibeforge-plus', token), // authenticated, with project
]).then(([unauth, authGeneric, authProject]) => {
  process.stdout.write(render(unauth, authGeneric, authProject));
}).catch(err => { console.error(err); process.exit(1); });

function fetchJson(url, bearerToken) {
  return new Promise((resolve, reject) => {
    const u = new URL(url);
    const headers = bearerToken ? { 'Authorization': 'Bearer ' + bearerToken } : {};
    const req = https.request({ hostname: u.hostname, path: u.pathname, method: 'GET', headers }, res => {
      let data = '';
      res.on('data', c => data += c);
      res.on('end', () => { try { resolve(JSON.parse(data)); } catch(e) { reject(e); } });
    });
    req.on('error', reject);
    req.end();
  });
}

function esc(s) {
  return String(s || '').replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;');
}

function render(unauth, authGeneric, authProject) {
  return `<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<title>VibeForge+ Agent Contract Reference — All Scenarios</title>
<link href="https://fonts.googleapis.com/css2?family=Plus+Jakarta+Sans:wght@400;600;700&family=JetBrains+Mono:wght@400;500&display=swap" rel="stylesheet">
<style>
:root{--bg:#0f172a;--bg2:rgba(124,58,237,0.04);--text:#e2e8f0;--text2:#cbd5e1;--text-muted:#94a3b8;--text-dim:#475569;--accent:#7c3aed;--accent2:#38bdf8;--border:rgba(124,58,237,0.1);--border2:rgba(124,58,237,0.15);--code-bg:rgba(124,58,237,0.08);--json-bg:rgba(15,23,42,0.6);--scenario-bg:rgba(124,58,237,0.06);--scenario-hover:rgba(124,58,237,0.1);--method-get:#10b981;--method-post:#f59e0b;--method-patch:#38bdf8;--method-put:#a78bfa;--method-del:#ef4444;--rule-border:rgba(124,58,237,0.15);--table-border:rgba(124,58,237,0.06);--table-th-border:rgba(124,58,237,0.2)}
[data-theme="light"]{--bg:#f8fafc;--bg2:rgba(124,58,237,0.03);--text:#1e293b;--text2:#334155;--text-muted:#64748b;--text-dim:#94a3b8;--accent:#7c3aed;--accent2:#2563eb;--border:rgba(124,58,237,0.08);--border2:rgba(124,58,237,0.12);--code-bg:rgba(124,58,237,0.06);--json-bg:rgba(241,245,249,0.8);--scenario-bg:rgba(124,58,237,0.03);--scenario-hover:rgba(124,58,237,0.06);--rule-border:rgba(124,58,237,0.1);--table-border:rgba(124,58,237,0.04);--table-th-border:rgba(124,58,237,0.15)}
*{box-sizing:border-box;margin:0;padding:0}
body{font-family:"Plus Jakarta Sans",sans-serif;background:var(--bg);color:var(--text);padding:2rem;max-width:1200px;margin:0 auto;line-height:1.6;transition:background 0.3s,color 0.3s}
h1{font-size:1.5rem;color:var(--accent);margin-bottom:0.3rem;text-shadow:0 0 20px rgba(124,58,237,0.3)}
h2{font-size:1.1rem;color:var(--accent2);margin:1.5rem 0 0.5rem;border-bottom:1px solid var(--border2);padding-bottom:0.3rem}
h3{font-size:0.9rem;color:var(--accent);margin:1rem 0 0.3rem}
.meta{font-family:"JetBrains Mono",monospace;font-size:0.7rem;color:var(--text-dim);margin-bottom:1.5rem}
.section{background:var(--bg2);border:1px solid var(--border);border-radius:8px;padding:1rem;margin-bottom:1rem}
.endpoint{font-family:"JetBrains Mono",monospace;font-size:0.75rem;color:var(--text2);padding:0.2rem 0;margin-bottom:0.5rem}
.method{font-weight:600;display:inline-block;width:55px}
.method-get{color:var(--method-get)}.method-post{color:var(--method-post)}.method-patch{color:var(--method-patch)}.method-put{color:var(--method-put)}.method-delete{color:var(--method-del)}
.rule{font-size:0.8rem;color:var(--text-muted);padding:0.3rem 0 0.3rem 1rem;border-left:2px solid var(--rule-border);margin-top:0.3rem}
.field{font-family:"JetBrains Mono",monospace;font-size:0.75rem;margin:0.2rem 0}
.field-name{color:var(--accent);font-weight:600}.field-desc{color:var(--text-muted)}
table{width:100%;border-collapse:collapse;font-size:0.8rem;margin:0.5rem 0}
th{text-align:left;color:var(--accent);font-size:0.7rem;text-transform:uppercase;letter-spacing:0.08em;padding:0.3rem 0.5rem;border-bottom:1px solid var(--table-th-border)}
td{padding:0.3rem 0.5rem;color:var(--text2);border-bottom:1px solid var(--table-border)}
code{font-family:"JetBrains Mono",monospace;font-size:0.75rem;background:var(--code-bg);padding:1px 4px;border-radius:3px}
.badge{font-family:"JetBrains Mono",monospace;font-size:0.6rem;padding:1px 6px;border-radius:4px;font-weight:600}
.badge-new{background:rgba(16,185,129,0.15);color:#10b981;border:1px solid rgba(16,185,129,0.3)}
.badge-warn{background:rgba(245,158,11,0.15);color:#f59e0b;border:1px solid rgba(245,158,11,0.3)}

/* ── Theme toggle ── */
.theme-toggle{position:fixed;top:1rem;right:1rem;z-index:100;display:flex;align-items:center;gap:0.5rem;font-family:"JetBrains Mono",monospace;font-size:0.65rem;color:var(--text-muted);background:var(--bg2);border:1px solid var(--border);border-radius:20px;padding:0.3rem 0.75rem;cursor:pointer;transition:all 0.2s;user-select:none}
.theme-toggle:hover{border-color:var(--accent);color:var(--accent)}
.theme-toggle-dot{width:14px;height:14px;border-radius:50%;background:var(--accent);transition:all 0.2s;box-shadow:0 0 6px rgba(124,58,237,0.3)}

/* ── Collapsible scenario panels ── */
.scenario{margin:1.5rem 0;border:1px solid var(--border2);border-radius:10px;overflow:hidden}
.scenario-header{display:flex;align-items:center;gap:0.75rem;padding:0.75rem 1.25rem;cursor:pointer;user-select:none;background:var(--scenario-bg);border-bottom:1px solid var(--border);transition:background 0.15s}
.scenario-header:hover{background:var(--scenario-hover)}
.scenario-toggle{font-size:0.7rem;color:var(--accent);transition:transform 0.2s;flex-shrink:0}
.scenario-header.open .scenario-toggle{transform:rotate(90deg)}
.scenario-title{font-family:"JetBrains Mono",monospace;font-size:0.85rem;font-weight:600;color:var(--text)}
.scenario-sub{font-family:"JetBrains Mono",monospace;font-size:0.65rem;color:var(--text-dim);margin-left:auto}
.scenario-body{display:none;padding:1.25rem}
.scenario-body.open{display:block}

.scenario-tag{font-family:"JetBrains Mono",monospace;font-size:0.55rem;padding:2px 8px;border-radius:4px;font-weight:600;text-transform:uppercase;letter-spacing:0.08em}
.tag-unauth{background:rgba(239,68,68,0.12);color:#ef4444;border:1px solid rgba(239,68,68,0.25)}
.tag-auth{background:rgba(16,185,129,0.12);color:#10b981;border:1px solid rgba(16,185,129,0.25)}
.tag-project{background:rgba(124,58,237,0.12);color:#7c3aed;border:1px solid rgba(124,58,237,0.25)}

.json-block{font-family:"JetBrains Mono",monospace;font-size:0.7rem;background:var(--json-bg);border:1px solid var(--border);border-radius:6px;padding:0.75rem 1rem;overflow-x:auto;color:var(--text2);white-space:pre-wrap;line-height:1.5}
.json-key{color:var(--accent)}.json-str{color:#10b981}.json-num{color:#f59e0b}.json-bool{color:var(--accent2)}.json-null{color:var(--text-dim)}

.divider{height:1px;background:linear-gradient(90deg,transparent,rgba(124,58,237,0.2),transparent);margin:1.5rem 0}
</style>
</head>
<body>
<div class="theme-toggle" onclick="toggleTheme()">
  <div class="theme-toggle-dot"></div>
  <span id="themeLabel">dark</span>
</div>
<h1>VibeForge+ Agent Contract Reference</h1>
<div class="meta">Contract v${authGeneric.contract_version} | Generated: ${new Date().toISOString().slice(0, 19)}Z | This document shows all contract scenarios an agent may encounter</div>

${renderScenario('1', 'Unauthenticated', 'GET /agentnotes (no token)', 'tag-unauth', 'UNAUTH',
  'What a brand new agent sees before it has a Bearer token. Minimal — just tells them how to authenticate.',
  unauth, true)}

${renderScenario('2', 'Authenticated — No Project', 'GET /agentnotes (with token)', 'tag-auth', 'AUTH',
  'Agent has authenticated but has not selected a project. Gets the full generic contract with {slug} placeholders. Sees available_projects to choose from. If empty, guided to help human create one.',
  authGeneric, false)}

${renderScenario('3', 'Authenticated — With Project', 'GET /agentnotes/vibeforge-plus (with token)', 'tag-project', 'PROJECT',
  'Agent has selected a project. Gets the full contract with project-specific context: resolved slugs, project documentation references, project-specific CLAUDE.md template.',
  authProject, false)}

<div class="divider"></div>

${renderContractDetail(authProject)}

<div class="meta" style="margin-top:2rem">Auto-generated from /agentnotes contract v${authGeneric.contract_version}</div>

<script>
document.querySelectorAll('.scenario-header').forEach(h => {
  h.addEventListener('click', () => {
    h.classList.toggle('open');
    h.nextElementSibling.classList.toggle('open');
  });
});

function toggleTheme() {
  var html = document.documentElement;
  var label = document.getElementById('themeLabel');
  if (html.getAttribute('data-theme') === 'light') {
    html.removeAttribute('data-theme');
    label.textContent = 'dark';
  } else {
    html.setAttribute('data-theme', 'light');
    label.textContent = 'light';
  }
}
</script>
</body>
</html>`;
}

function renderScenario(num, title, endpoint, tagCls, tagText, desc, data, startOpen) {
  const openCls = startOpen ? ' open' : '';
  return `
<div class="scenario">
  <div class="scenario-header${openCls}" id="scenario-${num}">
    <span class="scenario-toggle">&#9654;</span>
    <span class="scenario-tag ${tagCls}">${tagText}</span>
    <span class="scenario-title">${title}</span>
    <span class="scenario-sub">${endpoint}</span>
  </div>
  <div class="scenario-body${openCls}">
    <p style="font-size:0.8rem;color:#94a3b8;margin-bottom:1rem">${desc}</p>
    <div class="json-block">${syntaxHighlight(JSON.stringify(data, null, 2))}</div>
  </div>
</div>`;
}

function syntaxHighlight(json) {
  return esc(json)
    .replace(/"([^"]+)":/g, '<span class="json-key">"$1"</span>:')
    .replace(/: "([^"]*)"/g, ': <span class="json-str">"$1"</span>')
    .replace(/: (\d+)/g, ': <span class="json-num">$1</span>')
    .replace(/: (true|false)/g, ': <span class="json-bool">$1</span>')
    .replace(/: (null)/g, ': <span class="json-null">$1</span>');
}

function renderContractDetail(o) {
  let h = '<h2>Full Contract Breakdown (Project Mode)</h2>';

  // Board Capabilities
  if (o.board_capabilities) {
    h += '<h3>Board Capabilities <span class="badge badge-new">v2.2</span></h3><div class="section">';
    h += `<div class="field"><span class="field-desc">${esc(o.board_capabilities.summary)}</span></div>`;

    if (o.board_capabilities.structure) {
      h += '<h3 style="margin-top:0.75rem">Structure</h3>';
      for (const [k, v] of Object.entries(o.board_capabilities.structure)) {
        h += `<div style="margin:0.5rem 0 0.5rem 1rem">`;
        h += `<div class="field"><span class="field-name">${k}</span></div>`;
        if (typeof v === 'object') {
          for (const [fk, fv] of Object.entries(v)) {
            h += `<div class="rule"><span class="field-name" style="font-size:0.7rem">${fk}:</span> <span class="field-desc">${esc(String(fv))}</span></div>`;
          }
        } else {
          h += `<div class="rule"><span class="field-desc">${esc(v)}</span></div>`;
        }
        h += '</div>';
      }
    }

    if (o.board_capabilities.collaboration_model) {
      h += '<h3 style="margin-top:0.75rem">Collaboration Model</h3>';
      for (const [k, v] of Object.entries(o.board_capabilities.collaboration_model)) {
        h += `<div class="field"><span class="field-name">${k}:</span> <span class="field-desc">${esc(v)}</span></div>`;
      }
    }

    if (o.board_capabilities.planning_guidance) {
      h += '<h3 style="margin-top:0.75rem">Planning Guidance</h3>';
      h += `<div class="field"><span class="field-desc">${esc(o.board_capabilities.planning_guidance.description)}</span></div>`;
      for (const step of o.board_capabilities.planning_guidance.steps) {
        h += `<div class="rule">${esc(step)}</div>`;
      }
    }
    h += '</div>';
  }

  // Discovery
  if (o.discovery) {
    h += '<h3>Discovery (First Steps)</h3><div class="section">';
    for (const step of o.discovery) {
      h += `<div class="rule">${esc(step)}</div>`;
    }
    h += '</div>';
  }

  // Hierarchy
  h += '<h3>Hierarchy</h3><div class="section">';
  h += `<div class="field"><span class="field-desc">${esc(o.hierarchy.structure)}</span></div>`;
  for (const [k, v] of Object.entries(o.hierarchy)) {
    if (k === 'structure') continue;
    h += `<div class="field"><span class="field-name">${k}:</span> <span class="field-desc">${esc(v)}</span></div>`;
  }
  h += '</div>';

  // Endpoints
  h += '<h2>API Endpoints</h2>';
  for (const [group, eps] of Object.entries(o.endpoints)) {
    h += `<h3>${group.charAt(0).toUpperCase() + group.slice(1)}</h3><div class="section">`;
    for (const [name, ep] of Object.entries(eps)) {
      if (typeof ep === 'string') {
        const method = ep.split(' ')[0];
        h += `<div class="endpoint"><span class="method method-${method.toLowerCase()}">${method}</span> ${esc(ep.slice(method.length + 1))}</div>`;
      } else {
        const m = ep.method || 'GET';
        h += `<div class="endpoint">`;
        h += `<span class="method method-${m.toLowerCase()}">${m}</span> <code>${esc(ep.path)}</code>`;
        if (ep.description) h += `<div style="font-size:0.7rem;color:#94a3b8;margin:0.1rem 0 0 55px">${esc(ep.description)}</div>`;
        if (ep.body) h += `<div style="font-size:0.65rem;color:#475569;margin:0.1rem 0 0 55px">Body: <code>${esc(ep.body)}</code></div>`;
        if (ep.access) h += `<div style="font-size:0.6rem;margin:0.1rem 0 0 55px"><span class="badge badge-warn">${ep.access}</span></div>`;
        h += `</div>`;
      }
    }
    h += '</div>';
  }

  // Note Fields
  if (o.note_fields) {
    h += '<h2>Note Fields <span class="badge badge-new">v2.2</span></h2><div class="section">';
    for (const [k, v] of Object.entries(o.note_fields)) {
      h += `<div class="field"><span class="field-name">${k}:</span> <span class="field-desc">${esc(v)}</span></div>`;
    }
    h += '</div>';
  }

  // Agent Enforcement
  if (o.agent_enforcement) {
    h += '<h2>Agent Enforcement <span class="badge badge-new">v2.2</span></h2><div class="section">';
    h += `<div class="field"><span class="field-desc">${esc(o.agent_enforcement.summary)}</span></div>`;
    for (const r of o.agent_enforcement.rules) {
      h += `<div class="rule">${esc(r)}</div>`;
    }
    h += '</div>';
  }

  // Task Discipline
  h += '<h2>Task Discipline</h2><div class="section">';
  h += `<div class="field"><span class="field-desc">${esc(o.task_discipline.summary)}</span></div>`;
  for (const r of o.task_discipline.rules) {
    h += `<div class="rule">${esc(r)}</div>`;
  }
  h += '</div>';

  // Priority Matrix
  h += '<h2>Priority Matrix</h2><div class="section"><table><tr><th>Priority</th><th>SLA</th><th>Criteria</th><th>Action</th></tr>';
  for (const [k, v] of Object.entries(o.priority_matrix)) {
    h += `<tr><td><strong>${k}</strong></td><td>${esc(v.sla)}</td><td>${esc(v.criteria)}</td><td>${esc(v.action)}</td></tr>`;
  }
  h += '</table></div>';

  // Sync Expectations
  h += '<h2>Sync Expectations</h2><div class="section">';
  for (const [k, v] of Object.entries(o.sync_expectations)) {
    h += `<div class="field"><span class="field-name">${k}:</span> <span class="field-desc">${esc(v)}</span></div>`;
  }
  h += '</div>';

  // Board Reconciliation
  h += '<h2>Board Reconciliation</h2><div class="section">';
  h += `<div class="field"><span class="field-desc">${esc(o.board_reconciliation.summary)}</span></div>`;
  for (const r of o.board_reconciliation.rules) {
    h += `<div class="rule">${esc(r)}</div>`;
  }
  h += '</div>';

  // Audit Trail
  h += '<h2>Audit Trail</h2><div class="section">';
  h += `<div class="field"><span class="field-name">Tracked:</span> <span class="field-desc">${o.audit_trail.tracked_changes.join(', ')}</span></div>`;
  h += `<div class="field"><span class="field-name">Actors:</span> <span class="field-desc">${o.audit_trail.actor_types.join(', ')}</span></div>`;
  h += '</div>';

  // Project CRUD
  h += '<h2>Project CRUD</h2><div class="section">';
  for (const [name, ep] of Object.entries(o.project_crud)) {
    if (typeof ep === 'string') {
      const method = ep.split(' ')[0];
      h += `<div class="endpoint"><span class="method method-${method.toLowerCase()}">${method}</span> ${esc(ep.slice(method.length + 1))}</div>`;
    } else {
      const m = ep.method || 'GET';
      h += `<div class="endpoint"><span class="method method-${m.toLowerCase()}">${m}</span> <code>${esc(ep.path)}</code>`;
      if (ep.description) h += `<div style="font-size:0.7rem;color:#94a3b8;margin:0.1rem 0 0 55px">${esc(ep.description)}</div>`;
      h += '</div>';
    }
  }
  h += '</div>';

  // Bootstrap
  if (o.bootstrap) {
    h += '<h2>Bootstrap</h2><div class="section">';
    h += `<div class="field"><span class="field-desc">${esc(o.bootstrap.description)}</span></div>`;
    for (const step of o.bootstrap.steps) {
      h += `<div class="rule"><strong>Step ${step.step}:</strong> ${esc(step.action)} — ${esc(step.description)}</div>`;
    }
    h += '</div>';
  }

  // Workflows
  if (o.workflows) {
    h += '<h2>Workflows</h2>';
    for (const [name, wf] of Object.entries(o.workflows)) {
      h += `<h3>${name}</h3><div class="section">`;
      h += `<div class="field"><span class="field-desc">${esc(wf.description)}</span></div>`;
      if (Array.isArray(wf.steps)) {
        for (const step of wf.steps) {
          if (typeof step === 'string') {
            h += `<div class="rule">${esc(step)}</div>`;
          } else {
            h += `<div class="rule"><strong>${step.name}:</strong> ${esc(step.action)}</div>`;
          }
        }
      }
      h += '</div>';
    }
  }

  // Status Enum
  h += '<h2>Status Enum</h2><div class="section">';
  h += `<div class="field"><span class="field-desc">${o.status_enum.join(' → ')}</span></div>`;
  h += '</div>';

  return h;
}
