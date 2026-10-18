/* Plugboard — live routing view (Overview page).
 *
 * A three.js scene of the gateway: client keys on the outer ring, the
 * Plugboard hub in the middle, providers on the inner ring. Bright pulses are
 * real requests: /metrics is polled every few seconds and each new request on
 * a provider sends one pulse along client -> hub -> provider. Faint pulses are
 * a heartbeat and say so in the legend. "Showcase" opens a full-screen
 * version for screen recordings.
 *
 * three.js is vendored (static/vendor/three, r169, MIT): the admin dashboard
 * loads nothing from a CDN.
 */
import * as THREE from "./vendor/three/three.module.min.js";

const POLL_MS = 3000;
const MAX_PULSES = 220;
// Ink & champagne palette (matches style.css).
const COLORS = {
  hub: 0xcdb07a,
  cli: 0x7cc2c2,
  api: 0x7f9bd6,
  local: 0x7cc29a,
  down: 0xe8907f,
  client: 0xe2c992,
};
const reducedMotion = window.matchMedia("(prefers-reduced-motion: reduce)").matches;

async function getJSON(path) {
  const res = await fetch(path, { credentials: "same-origin", headers: { Accept: "application/json" } });
  if (!res.ok) throw new Error(path + " -> " + res.status);
  return res.json();
}

function providerKind(p) {
  const auth = p.auth_source || "";
  if (p.is_active === false) return "down";
  if (auth.startsWith("subprocess:") || auth.startsWith("cli:") || auth.startsWith("broker:")) return "cli";
  if ((p.id || "").startsWith("ollama") || (p.base_url || "").includes("11434")) return "local";
  return "api";
}

function labelSprite(text, sub, color) {
  const canvas = document.createElement("canvas");
  const ratio = 2;
  canvas.width = 512 * ratio;
  canvas.height = 128 * ratio;
  const ctx = canvas.getContext("2d");
  ctx.scale(ratio, ratio);
  ctx.font = "500 34px Inter, system-ui, sans-serif";
  ctx.textAlign = "center";
  ctx.shadowColor = "rgba(0,0,0,0.6)";
  ctx.shadowBlur = 8;
  ctx.fillStyle = "#" + new THREE.Color(color).lerp(new THREE.Color(0xffffff), 0.35).getHexString();
  ctx.fillText(text.length > 26 ? text.slice(0, 25) + "…" : text, 256, 52);
  if (sub) {
    ctx.font = "400 24px Inter, system-ui, sans-serif";
    ctx.fillStyle = "rgba(236,230,216,0.7)";
    ctx.fillText(sub, 256, 90);
  }
  const texture = new THREE.CanvasTexture(canvas);
  texture.colorSpace = THREE.SRGBColorSpace;
  const sprite = new THREE.Sprite(new THREE.SpriteMaterial({ map: texture, transparent: true, depthWrite: false }));
  sprite.scale.set(4, 1, 1);
  return sprite;
}

function glowTexture() {
  const size = 128;
  const canvas = document.createElement("canvas");
  canvas.width = canvas.height = size;
  const ctx = canvas.getContext("2d");
  const g = ctx.createRadialGradient(size / 2, size / 2, 0, size / 2, size / 2, size / 2);
  g.addColorStop(0, "rgba(255,255,255,1)");
  g.addColorStop(0.25, "rgba(255,255,255,0.55)");
  g.addColorStop(1, "rgba(255,255,255,0)");
  ctx.fillStyle = g;
  ctx.fillRect(0, 0, size, size);
  const texture = new THREE.CanvasTexture(canvas);
  texture.colorSpace = THREE.SRGBColorSpace;
  return texture;
}

class LiveRouting {
  constructor(container) {
    this.container = container;
    this.canvasHost = container.querySelector(".live3d-canvas");
    this.stats = container.querySelector(".live3d-stats");
    this.scene = new THREE.Scene();
    this.scene.fog = new THREE.FogExp2(0x07080b, 0.016);
    this.camera = new THREE.PerspectiveCamera(45, 1, 0.1, 200);
    this.camera.position.set(0, 6.5, 18);
    this.renderer = new THREE.WebGLRenderer({ antialias: true, alpha: true, powerPreference: "high-performance" });
    this.renderer.setPixelRatio(Math.min(window.devicePixelRatio || 1, 2));
    this.canvasHost.appendChild(this.renderer.domElement);
    this.glow = glowTexture();
    this.world = new THREE.Group();
    this.scene.add(this.world);
    this.nodes = new Map(); // provider id -> { position, color }
    this.clients = [];
    this.pulses = [];
    this.lastCounts = null;
    this.pointer = { x: 0, y: 0 };
    this.clock = new THREE.Clock();
    this.visible = true;
    this.heartbeatAt = 0;
    this.buildBackdrop();
    this.buildHub();
    this.bindEvents();
    this.resize();
  }

  buildBackdrop() {
    const count = 1400;
    const positions = new Float32Array(count * 3);
    for (let i = 0; i < count; i++) {
      const r = 30 + Math.random() * 50;
      const theta = Math.random() * Math.PI * 2;
      const phi = Math.acos(2 * Math.random() - 1);
      positions[i * 3] = r * Math.sin(phi) * Math.cos(theta);
      positions[i * 3 + 1] = r * Math.cos(phi) * 0.6;
      positions[i * 3 + 2] = r * Math.sin(phi) * Math.sin(theta);
    }
    const geometry = new THREE.BufferGeometry();
    geometry.setAttribute("position", new THREE.Float32BufferAttribute(positions, 3));
    this.stars = new THREE.Points(geometry, new THREE.PointsMaterial({
      size: 0.1, color: 0xb9b2a2, transparent: true, opacity: 0.55, depthWrite: false,
    }));
    this.scene.add(this.stars);

    const grid = new THREE.PolarGridHelper(16, 24, 10, 96, 0x2a261d, 0x2a261d);
    grid.position.y = -2.6;
    grid.material.transparent = true;
    grid.material.opacity = 0.45;
    this.world.add(grid);
  }

  buildHub() {
    this.hub = new THREE.Group();
    const core = new THREE.Mesh(
      new THREE.IcosahedronGeometry(1.05, 3),
      new THREE.MeshBasicMaterial({ color: COLORS.hub }),
    );
    const shell = new THREE.Mesh(
      new THREE.IcosahedronGeometry(1.75, 1),
      new THREE.MeshBasicMaterial({ color: 0xe2c992, wireframe: true, transparent: true, opacity: 0.3 }),
    );
    const halo = new THREE.Sprite(new THREE.SpriteMaterial({
      map: this.glow, color: COLORS.hub, transparent: true, opacity: 0.55,
      blending: THREE.AdditiveBlending, depthWrite: false,
    }));
    halo.scale.set(4.6, 4.6, 1);
    const ring = new THREE.Mesh(
      new THREE.TorusGeometry(2.4, 0.025, 8, 160),
      new THREE.MeshBasicMaterial({ color: 0xb3955e, transparent: true, opacity: 0.55 }),
    );
    ring.rotation.x = Math.PI / 2.4;
    this.hubShell = shell;
    this.hubRing = ring;
    this.hub.add(core, shell, halo, ring);
    const label = labelSprite("Plugboard", "OpenAI-compatible gateway", COLORS.hub);
    label.position.set(0, 2.9, 0);
    label.scale.set(5, 1.25, 1);
    this.hub.add(label);
    this.world.add(this.hub);
  }

  node(position, color, size) {
    const group = new THREE.Group();
    const sphere = new THREE.Mesh(new THREE.SphereGeometry(size, 24, 16), new THREE.MeshBasicMaterial({ color }));
    const glow = new THREE.Sprite(new THREE.SpriteMaterial({
      map: this.glow, color, transparent: true, opacity: 0.55,
      blending: THREE.AdditiveBlending, depthWrite: false,
    }));
    glow.scale.set(size * 4.2, size * 4.2, 1);
    group.add(sphere, glow);
    group.position.copy(position);
    return group;
  }

  link(from, to, color, opacity) {
    const mid = from.clone().add(to).multiplyScalar(0.5);
    mid.y += 1.6 + from.distanceTo(to) * 0.08;
    const curve = new THREE.QuadraticBezierCurve3(from.clone(), mid, to.clone());
    const geometry = new THREE.BufferGeometry().setFromPoints(curve.getPoints(48));
    const line = new THREE.Line(geometry, new THREE.LineBasicMaterial({
      color, transparent: true, opacity, blending: THREE.AdditiveBlending, depthWrite: false,
    }));
    this.links.add(line);
    return curve;
  }

  layout(providers, clientIds, modelCounts) {
    if (this.layer) this.world.remove(this.layer);
    this.layer = new THREE.Group();
    this.links = new THREE.Group();
    this.layer.add(this.links);
    this.world.add(this.layer);
    this.nodes.clear();
    this.clients = [];

    const n = Math.max(providers.length, 1);
    providers.forEach((p, i) => {
      const angle = (i / n) * Math.PI * 2 + Math.PI / 2;
      const position = new THREE.Vector3(Math.cos(angle) * 7.2, -1.2 + Math.sin(i * 1.7) * 0.6, Math.sin(angle) * 7.2);
      const kind = providerKind(p);
      const color = COLORS[kind];
      const group = this.node(position, color, 0.42);
      const models = modelCounts[p.id] || 0;
      const label = labelSprite(p.name || p.id, models ? models + " models" : kind === "down" ? "inactive" : "", color);
      label.position.set(0, 1.05, 0);
      label.scale.set(4.6, 1.15, 1);
      group.add(label);
      this.layer.add(group);
      const curve = this.link(new THREE.Vector3(0, 0, 0), position, color, 0.28);
      this.nodes.set(p.id, { position, color, curve, group, phase: Math.random() * Math.PI * 2 });
    });

    const ids = clientIds.length ? clientIds.slice(0, 10) : ["your app"];
    ids.forEach((id, i) => {
      const angle = (i / ids.length) * Math.PI * 2 + Math.PI / ids.length;
      const position = new THREE.Vector3(Math.cos(angle) * 11.5, 2.6 + Math.cos(i * 2.3) * 0.8, Math.sin(angle) * 13);
      const group = this.node(position, COLORS.client, 0.28);
      const label = labelSprite(id, "client key", COLORS.client);
      label.position.set(0, 0.95, 0);
      label.scale.set(3.2, 0.8, 1);
      group.add(label);
      this.layer.add(group);
      const curve = this.link(position, new THREE.Vector3(0, 0, 0), COLORS.client, 0.16);
      this.clients.push({ id, position, curve });
    });
  }

  spawnPulse(providerId, live) {
    if (this.pulses.length >= MAX_PULSES) return;
    const target = this.nodes.get(providerId);
    if (!target || !this.clients.length) return;
    const client = this.clients[Math.floor(Math.random() * this.clients.length)];
    const sprite = new THREE.Sprite(new THREE.SpriteMaterial({
      map: this.glow, color: live ? target.color : 0x6b665c, transparent: true,
      opacity: live ? 1 : 0.45, blending: THREE.AdditiveBlending, depthWrite: false,
    }));
    const size = live ? 0.9 : 0.45;
    sprite.scale.set(size, size, 1);
    this.world.add(sprite);
    this.pulses.push({ sprite, legs: [client.curve, target.curve], t: 0, speed: live ? 0.75 : 0.45 });
  }

  updatePulses(dt) {
    for (let i = this.pulses.length - 1; i >= 0; i--) {
      const pulse = this.pulses[i];
      pulse.t += dt * pulse.speed;
      const leg = pulse.t < 1 ? 0 : 1;
      const local = leg === 0 ? pulse.t : pulse.t - 1;
      if (pulse.t >= 2) {
        this.world.remove(pulse.sprite);
        pulse.sprite.material.dispose();
        this.pulses.splice(i, 1);
        continue;
      }
      pulse.sprite.position.copy(pulse.legs[leg].getPoint(Math.min(local, 1)));
    }
  }

  async refresh() {
    const [providers, models, keys] = await Promise.all([
      getJSON("/providers").catch(() => []),
      getJSON("/v1/models").catch(() => ({ data: [] })),
      getJSON("/admin/keys").catch(() => []),
    ]);
    const list = Array.isArray(providers) ? providers : providers.providers || [];
    const counts = {};
    (models.data || []).forEach((m) => {
      const owner = m.owned_by || m.provider || "";
      counts[owner] = (counts[owner] || 0) + 1;
    });
    const keyList = Array.isArray(keys) ? keys : keys.keys || [];
    const clientIds = [...new Set(keyList.filter((k) => k.is_active !== false).map((k) => k.client_id).filter(Boolean))];
    const signature = JSON.stringify([list.map((p) => [p.id, p.is_active, counts[p.id]]), clientIds]);
    if (signature !== this.signature) {
      this.signature = signature;
      this.layout(list, clientIds, counts);
    }
    this.statValues = [list.length, (models.data || []).length, clientIds.length];
    this.renderStats();
  }

  renderStats() {
    if (!this.stats || !this.statValues) return;
    const [providers, models, clients] = this.statValues;
    this.stats.replaceChildren(...[
      [providers, "providers"], [models, "models"], [clients, "client keys"], [this.liveTotal || 0, "requests seen live"],
    ].map(([value, label]) => {
      const chip = document.createElement("div");
      chip.className = "live3d-chip";
      const strong = document.createElement("strong");
      strong.textContent = String(value);
      const span = document.createElement("span");
      span.textContent = label;
      chip.append(strong, span);
      return chip;
    }));
  }

  async poll() {
    try {
      const metrics = await getJSON("/metrics");
      const counts = {};
      Object.keys(metrics || {}).forEach((pid) => { counts[pid] = Number(metrics[pid].requests || 0); });
      if (this.lastCounts) {
        Object.keys(counts).forEach((pid) => {
          const delta = Math.min(counts[pid] - (this.lastCounts[pid] || 0), 12);
          for (let i = 0; i < delta; i++) setTimeout(() => this.spawnPulse(pid, true), i * 140);
          if (delta > 0) this.liveTotal = (this.liveTotal || 0) + delta;
        });
        this.renderStats();
      }
      this.lastCounts = counts;
    } catch (err) {
      /* metrics unavailable: keep the heartbeat only */
    }
  }

  bindEvents() {
    this.onResize = () => this.resize();
    window.addEventListener("resize", this.onResize);
    this.container.addEventListener("pointermove", (ev) => {
      const rect = this.container.getBoundingClientRect();
      this.pointer.x = ((ev.clientX - rect.left) / rect.width) * 2 - 1;
      this.pointer.y = ((ev.clientY - rect.top) / rect.height) * 2 - 1;
    });
    new IntersectionObserver((entries) => {
      this.visible = entries.some((e) => e.isIntersecting);
    }).observe(this.container);
    document.addEventListener("visibilitychange", () => {
      if (!document.hidden) this.clock.getDelta();
    });
  }

  resize() {
    const rect = this.canvasHost.getBoundingClientRect();
    const width = Math.max(rect.width, 1);
    const height = Math.max(rect.height, 1);
    this.camera.aspect = width / height;
    this.camera.position.z = width < 640 ? 28 : 18;
    this.camera.updateProjectionMatrix();
    this.renderer.setSize(width, height, false);
  }

  frame() {
    const dt = Math.min(this.clock.getDelta(), 0.05);
    const t = this.clock.elapsedTime;
    if (!reducedMotion) {
      this.world.rotation.y += dt * 0.08;
      this.hubShell.rotation.y -= dt * 0.35;
      this.hubShell.rotation.x += dt * 0.12;
      this.hubRing.rotation.z += dt * 0.5;
      this.stars.rotation.y += dt * 0.004;
      this.nodes.forEach((n) => {
        n.group.position.y = n.position.y + Math.sin(t * 1.2 + n.phase) * 0.12;
      });
      if (t - this.heartbeatAt > 0.9 && this.nodes.size) {
        this.heartbeatAt = t;
        const ids = [...this.nodes.keys()];
        this.spawnPulse(ids[Math.floor(Math.random() * ids.length)], false);
      }
      this.updatePulses(dt);
    }
    if (!reducedMotion) {
      this.camera.position.x += (this.pointer.x * 2.2 - this.camera.position.x) * 0.04;
      this.camera.position.y += (6.5 - this.pointer.y * 1.8 - this.camera.position.y) * 0.04;
    }
    this.camera.lookAt(0, 0.3, 0);
    this.renderer.render(this.scene, this.camera);
  }

  start() {
    const loop = () => {
      if (this.visible && !document.hidden) this.frame();
      requestAnimationFrame(loop);
    };
    requestAnimationFrame(loop);
    this.refresh().then(() => this.poll());
    setInterval(() => { if (!document.hidden) this.poll(); }, POLL_MS);
    setInterval(() => { if (!document.hidden) this.refresh(); }, 30000);
  }
}

function webglAvailable() {
  try {
    const canvas = document.createElement("canvas");
    return !!(canvas.getContext("webgl2") || canvas.getContext("webgl"));
  } catch (err) {
    return false;
  }
}

function init() {
  const container = document.getElementById("live3d");
  if (!container) return;
  if (!webglAvailable()) {
    container.classList.add("live3d-unavailable");
    return;
  }
  const view = new LiveRouting(container);
  view.start();

  const button = container.querySelector(".live3d-showcase");
  const toggle = (on) => {
    container.classList.toggle("live3d-fullscreen", on);
    document.body.classList.toggle("live3d-locked", on);
    button.setAttribute("aria-pressed", on ? "true" : "false");
    button.textContent = on ? "Exit showcase" : "Showcase";
    requestAnimationFrame(() => view.resize());
  };
  button.addEventListener("click", () => toggle(!container.classList.contains("live3d-fullscreen")));
  document.addEventListener("keydown", (ev) => {
    if (ev.key === "Escape" && container.classList.contains("live3d-fullscreen")) toggle(false);
  });
}

init();
