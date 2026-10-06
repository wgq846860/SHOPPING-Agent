/* =============================================================================
 * Hero 区 3D 头戴式耳机（UI-REV-05 重做建模；UI-REV-04 渲染策略保持）
 * -----------------------------------------------------------------------------
 * 用 three.js 程序化建模一副头戴式（over-ear）耳机，替换原 🎧 emoji 装饰。
 *   - 交互：鼠标/触屏按住拖动即可旋转（横向绕 Y、纵向俯仰，带惯性）；
 *     不放任何文字指引，用 grab 光标与闲置自转表达可交互。
 *   - 渲染策略：永续 rAF 循环 + 闲置 2.4s 缓慢自转（UI-REV-03 曾改为按需
 *     渲染以省电，用户反馈"观感有点差"，UI-REV-04 恢复本版；浏览器对隐藏
 *     标签页会自动暂停 rAF，切走时不会白烧）。
 *   - 无障碍：prefers-reduced-motion 时关闭闲置自转（仍可手动拖动）。
 *   - 回退：WebGL 不可用或资源加载失败时，保留容器内原有 emoji 占位。
 *   - 资源：three.js 为本地 vendor 文件（module + core 两个文件），
 *     经 index.html 的 importmap 引入，演示环境无需联网。
 *
 * 建模尺寸与防穿模约束：
 *   - 弧带中心半径 0.84，内衬外缘 0.76 + 0.038 = 0.798，比
 *     外带内缘 0.84 - 0.048 = 0.792 多 0.006，属于缝合搭接；
 *   - 两耳罩中心 x=±0.88、y=-0.53，外壳半宽 0.16；耳垫中心
 *     x=±0.72，厚度向内伸展，和壳体内缘 x=±0.72 仅作装配搭接；
 *   - 中央滑轨从头梁末端向下插入耳罩顶部；耳罩高度范围内的支架
 *     顶点保持在椭圆壳体内部，避免前后露出叉臂端点；
 *   - 局部最远点距原点小于 1.45，模型缩放为 0.98；绕 Y 旋转为
 *     刚体变换，不会产生新的静态相交。
 * 挂载点：#hero-phone（index.html Hero 区右侧圆底）。容器内 emoji 仅作回退。
 * ========================================================================== */

import * as THREE from "three";

const MOUNT_SELECTOR = "#hero-phone";

/* ------------------------------ 材质定义 ------------------------------ */

function buildMaterials() {
  return {
    shell: new THREE.MeshStandardMaterial({ color: 0x8193ad, roughness: 0.34, metalness: 0.18, emissive: 0x2f3d55, emissiveIntensity: 0.18 }),
    face: new THREE.MeshStandardMaterial({ color: 0x536a89, roughness: 0.28, metalness: 0.25, emissive: 0x334b6f, emissiveIntensity: 0.15 }),
    band: new THREE.MeshStandardMaterial({ color: 0x7e8fa7, roughness: 0.56, metalness: 0.12 }),
    metal: new THREE.MeshStandardMaterial({ color: 0xd6dfed, roughness: 0.25, metalness: 0.3 }),
    accent: new THREE.MeshStandardMaterial({ color: 0xb6d1f4, roughness: 0.28, metalness: 0.2 }),
    cushion: new THREE.MeshStandardMaterial({ color: 0x2b3444, roughness: 0.96, metalness: 0 }),
    cloth: new THREE.MeshStandardMaterial({ color: 0x182335, roughness: 1, metalness: 0, side: THREE.DoubleSide }),
  };
}

/* ------------------------------ 几何搭建 ------------------------------ */

const BAND_R = 0.84;
const BAND_TUBE = 0.048;
const BAND_ARC = Math.PI * 0.94;
const CUP_X = 0.88;
const CUP_Y = -0.53;

function bandEndX() { return BAND_R * Math.sin(BAND_ARC / 2); }
function bandEndY() { return BAND_R * Math.cos(BAND_ARC / 2) + 0.08; }

function buildHeadband(mats) {
  const group = new THREE.Group();
  const outer = new THREE.Mesh(
    new THREE.TorusGeometry(BAND_R, BAND_TUBE, 16, 96, BAND_ARC),
    mats.band,
  );
  outer.scale.z = 2.65; // 深度方向宽 0.254，正面看是厚实的头梁
  outer.rotation.z = Math.PI / 2 - BAND_ARC / 2;
  outer.position.y = 0.08;
  group.add(outer);

  const pad = new THREE.Mesh(
    new THREE.TorusGeometry(0.76, 0.038, 14, 72, BAND_ARC * 0.72),
    mats.cushion,
  );
  pad.scale.z = 2.6;
  pad.rotation.z = Math.PI / 2 - BAND_ARC * 0.72 / 2;
  pad.position.y = 0.08;
  group.add(pad);

  for (const side of [1, -1]) {
    const end = new THREE.Mesh(new THREE.SphereGeometry(BAND_TUBE, 16, 12), mats.band);
    end.scale.z = 2.65;
    end.position.set(side * bandEndX(), bandEndY(), 0);
    group.add(end);
  }
  return group;
}

function buildCup(mats, side) {
  const group = new THREE.Group();
  const sphere = new THREE.SphereGeometry(1, 48, 32);
  const shell = new THREE.Mesh(sphere, mats.shell);
  shell.scale.set(0.16, 0.44, 0.32);
  group.add(shell);

  // 外侧面板嵌入外壳，外圈和中心徽点是同轴装配件。
  const face = new THREE.Mesh(sphere, mats.face);
  face.position.x = side * 0.138;
  face.scale.set(0.035, 0.365, 0.255);
  group.add(face);
  const trim = new THREE.Mesh(new THREE.TorusGeometry(1, 0.065, 12, 56), mats.accent);
  trim.rotation.y = Math.PI / 2;
  trim.position.x = side * 0.176;
  trim.scale.set(0.239, 0.338, 0.55);
  group.add(trim);
  const mark = new THREE.Mesh(new THREE.SphereGeometry(1, 16, 12), mats.metal);
  mark.position.x = side * 0.177;
  mark.scale.set(0.012, 0.048, 0.048);
  group.add(mark);

  // 开口朝头部；耳垫与壳体内缘局部搭接，中心布面后退形成可见的凹腔。
  const cushion = new THREE.Mesh(new THREE.TorusGeometry(1, 0.18, 16, 64), mats.cushion);
  cushion.rotation.y = Math.PI / 2;
  cushion.position.x = -side * 0.16;
  cushion.scale.set(0.28, 0.38, 0.45);
  group.add(cushion);
  const cloth = new THREE.Mesh(new THREE.CircleGeometry(1, 48), mats.cloth);
  cloth.rotation.y = side * Math.PI / 2;
  cloth.position.x = -side * 0.119;
  cloth.scale.set(0.232, 0.322, 1);
  group.add(cloth);
  return group;
}

function buildArm(mats, side) {
  const group = new THREE.Group();
  const slider = new THREE.Mesh(new THREE.CapsuleGeometry(0.038, 0.40, 6, 12), mats.metal);
  slider.position.set(side * 0.837, -0.08, 0);
  group.add(slider);
  return group;
}

function buildHeadphones() {
  const mats = buildMaterials();
  const model = new THREE.Group();

  model.add(buildHeadband(mats));
  model.add(buildArm(mats, 1));
  model.add(buildArm(mats, -1));

  for (const side of [1, -1]) {
    const cup = buildCup(mats, side);
    cup.position.set(side * CUP_X, CUP_Y, 0);
    model.add(cup);
  }

  model.scale.setScalar(0.98);
  return model;
}

/* ------------------------------ 场景与交互 ------------------------------ */

function createViewer(container) {
  let renderer;
  try {
    renderer = new THREE.WebGLRenderer({ alpha: true, antialias: true });
  } catch {
    return false; // WebGL 不可用：保留 emoji 回退
  }
  renderer.setClearColor(0x000000, 0);

  const scene = new THREE.Scene();
  const camera = new THREE.PerspectiveCamera(30, 1, 0.1, 50);
  camera.position.set(0, 0.1, 4.85);
  camera.lookAt(0, 0, 0);

  // 三点布光 + 半球环境光：冷色轮廓光把模型从深色背景里"剥"出来
  scene.add(new THREE.HemisphereLight(0xdfe8ff, 0x141b28, 1.05));
  const key = new THREE.DirectionalLight(0xffffff, 2.0);
  key.position.set(2.2, 3.2, 4.2);
  scene.add(key);
  const rim = new THREE.DirectionalLight(0x7fa8ff, 1.6);
  rim.position.set(-3.2, 1.4, -4.0);
  scene.add(rim);
  const fill = new THREE.DirectionalLight(0xffe9c9, 0.4);
  fill.position.set(0.5, -2.5, 3.0);
  scene.add(fill);

  const root = new THREE.Group();
  root.add(buildHeadphones());
  scene.add(root);

  // 初始 3/4 视角
  const pose = { yaw: -0.45, pitch: 0.08 };
  const motion = { yawVel: 0, pitchVel: 0 };
  const drag = { active: false, x: 0, y: 0, lastAt: 0 };
  const reduceMotion = window.matchMedia("(prefers-reduced-motion: reduce)").matches;

  const canvas = renderer.domElement;
  canvas.setAttribute("aria-label", "头戴式耳机 3D 模型，可拖动旋转");

  // ---- 指针交互（鼠标与触屏统一走 Pointer Events） ----
  canvas.addEventListener("pointerdown", (e) => {
    drag.active = true;
    drag.x = e.clientX;
    drag.y = e.clientY;
    drag.lastAt = performance.now();
    motion.yawVel = 0;
    motion.pitchVel = 0;
    canvas.setPointerCapture(e.pointerId);
  });
  canvas.addEventListener("pointermove", (e) => {
    if (!drag.active) return;
    const dx = e.clientX - drag.x;
    const dy = e.clientY - drag.y;
    drag.x = e.clientX;
    drag.y = e.clientY;
    drag.lastAt = performance.now();
    const k = 0.0072;
    pose.yaw += dx * k;
    pose.pitch = THREE.MathUtils.clamp(pose.pitch + dy * k * 0.7, -0.55, 0.75);
    motion.yawVel = dx * k;
    motion.pitchVel = dy * k * 0.7;
  });
  const endDrag = (e) => {
    if (!drag.active) return;
    drag.active = false;
    drag.lastAt = performance.now();
    if (e && canvas.hasPointerCapture(e.pointerId)) canvas.releasePointerCapture(e.pointerId);
  };
  canvas.addEventListener("pointerup", endDrag);
  canvas.addEventListener("pointercancel", endDrag);

  // ---- 尺寸自适应 ----
  const fit = () => {
    const w = Math.max(1, canvas.clientWidth || container.clientWidth);
    const h = Math.max(1, canvas.clientHeight || container.clientHeight);
    renderer.setPixelRatio(Math.min(window.devicePixelRatio || 1, 2));
    renderer.setSize(w, h, false);
    camera.aspect = w / h;
    camera.updateProjectionMatrix();
  };
  if (typeof ResizeObserver !== "undefined") {
    new ResizeObserver(fit).observe(container);
  } else {
    window.addEventListener("resize", fit);
  }

  // ---- 渲染循环：拖拽 → 惯性衰减 → 闲置 2.4s 后缓慢自转 ----
  const IDLE_MS = 2400;
  const tick = () => {
    const now = performance.now();
    if (!drag.active) {
      pose.yaw += motion.yawVel;
      pose.pitch = THREE.MathUtils.clamp(pose.pitch + motion.pitchVel, -0.55, 0.75);
      motion.yawVel *= 0.93;
      motion.pitchVel *= 0.9;
      if (!reduceMotion && now - drag.lastAt > IDLE_MS) {
        pose.yaw += 0.0038;
      }
    }
    root.rotation.y = pose.yaw;
    root.rotation.x = pose.pitch;
    renderer.render(scene, camera);
    requestAnimationFrame(tick);
  };

  // 挂载：成功后才移除 emoji 回退
  container.textContent = "";
  container.classList.add("hp3d-on");
  container.appendChild(canvas);
  fit();
  requestAnimationFrame(tick);
  return true;
}

/* ------------------------------ 入口 ------------------------------ */

function mount(selector = MOUNT_SELECTOR) {
  const container = document.querySelector(selector);
  if (!container || container.dataset.hp3d === "mounted") return false;
  const ok = createViewer(container);
  if (ok) container.dataset.hp3d = "mounted";
  return ok;
}

window.Headphone3D = { mount };

if (document.readyState === "loading") {
  document.addEventListener("DOMContentLoaded", () => mount());
} else {
  mount();
}
