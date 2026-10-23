/** 폼 중복 클릭·본문 복사·원본 미리보기·키보드 지원 상품 보존 영역을 관리한다. */
// 원본 그림을 유지하고 표시 배율에 맞춘 고품질 축소로 작은 로고의 계단 현상을 줄인다.
const fishImage = new Image();
fishImage.addEventListener('load', () => {
  for (const canvas of document.querySelectorAll('canvas.fish-character')) {
    const draw = () => {
      const width = canvas.getBoundingClientRect().width;
      if (!width) return;
      const crop = canvas.dataset.crop.split(',').map(Number);
      const ratio = window.devicePixelRatio || 1;
      canvas.width = Math.round(width * ratio);
      canvas.height = Math.round(width * crop[3] / crop[2] * ratio);
      const ctx = canvas.getContext('2d');
      ctx.imageSmoothingEnabled = true;
      ctx.imageSmoothingQuality = 'high';
      ctx.drawImage(fishImage, ...crop, 0, 0, canvas.width, canvas.height);
      canvas.style.backgroundImage = 'none';
    };
    new ResizeObserver(draw).observe(canvas);
    window.addEventListener('resize', draw);
    draw();
  }
});
fishImage.src = '/static/fish-characters.png';

document.addEventListener('submit', event => {
    const form = event.target;
    if (!(form instanceof HTMLFormElement) || form.method.toLowerCase() !== 'post') return;
    if (form.dataset.confirm && !window.confirm(form.dataset.confirm)) { event.preventDefault(); return; }
    let points = [];
    if (form.id === 'image-generation') {
      try { points = JSON.parse(document.getElementById('mask-points').value); } catch { /* 아래 검증에서 안내한다. */ }
    }
    if (form.id === 'image-generation' && (!Array.isArray(points) || points.length < 3)) {
      event.preventDefault();
      document.getElementById('mask-message').textContent = '상품 영역을 표시하세요';
      document.getElementById('image-block').textContent = '상품 영역을 표시하세요';
      document.getElementById('mask-editor').focus();
      return;
    }
    if (form.id === 'image-generation' && document.getElementById('mask-mode')?.value === 'auto' && !document.getElementById('mask-png').value) {
      event.preventDefault();
      document.getElementById('mask-message').textContent = '상품 자동 선택을 누르세요';
      document.getElementById('image-block').textContent = '상품 자동 선택을 누르세요';
      document.getElementById('segment-product').focus();
      return;
    }
    if (form.id === 'image-generation' && !form.elements.confirmed.checked) {
      event.preventDefault();
      document.getElementById('image-block').textContent = '생성 동의가 필요해요';
      form.elements.confirmed.focus();
      return;
    }
    if (form.dataset.submitting) { event.preventDefault(); return; }
    form.dataset.submitting = 'yes';
    form.setAttribute('aria-busy', 'true');
    if (form.id === 'image-generation') { document.getElementById('image-block').textContent = ''; document.getElementById('image-recheck').hidden = true; showImageProgress(); }
    const button = form.querySelector('button[type="submit"]');
    if (button) { button.disabled = true; button.textContent = '처리 중…'; }
    if (form.id === 'image-generation') {
      event.preventDefault();
      submitImageGeneration(form);
    }
});
window.addEventListener('pageshow', event => {
  if (event.persisted) window.location.reload();
});
document.getElementById('copy-caption')?.addEventListener('click', async () => {
  const caption = document.getElementById('caption');
  const text = [caption.value, document.getElementById('hashtags').value].filter(Boolean).join('\n\n');
  try {
    await navigator.clipboard.writeText(text);
    document.getElementById('copy-message').textContent = '본문·태그 복사 완료';
  } catch {
    caption.focus(); caption.select();
    document.getElementById('copy-message').textContent = '선택된 본문을 복사하세요';
  }
});
let photoUrl;
document.getElementById('photo')?.addEventListener('change', event => {
  const file = event.target.files[0];
  const image = document.getElementById('local-photo');
  if (photoUrl) URL.revokeObjectURL(photoUrl);
  image.hidden = true;
  if (!file) return;
  if (file.size > 8 * 1024 * 1024) {
    event.target.value = '';
    document.getElementById('file-message').textContent = '사진은 8MB 이하';
    return;
  }
  document.getElementById('file-message').textContent = file.name;
  photoUrl = URL.createObjectURL(file); image.src = photoUrl; image.hidden = false;
});

const maskCanvas = document.getElementById('mask-editor');
const maskSource = document.getElementById('mask-source');
if (maskCanvas && maskSource) {
  const context = maskCanvas.getContext('2d');
  const mode = document.getElementById('mask-mode');
  const maskInput = document.getElementById('mask-png');
  const cutout = document.getElementById('mask-cutout');
  const message = document.getElementById('mask-message');
  const segmentButton = document.getElementById('segment-product');
  const tool = document.getElementById('mask-tool');
  const refine = document.getElementById('mask-refine');
  let selectionVersion = 0;
  let points = [], corrections = [], cursor = [0.5, 0.5], keyboard = false;
  function clearAutomaticMask() {
    selectionVersion++;
    maskInput.value = ''; cutout.hidden = true;
    document.querySelector('#image-generation input[name="confirmed"]').checked = false;
  }
  function drawMask() {
    if (!maskSource.complete || !maskSource.naturalWidth) return;
    context.drawImage(maskSource, 0, 0, maskCanvas.width, maskCanvas.height);
    if (points.length) {
      context.beginPath();
      points.forEach(([x, y], index) => context[index ? 'lineTo' : 'moveTo'](x * maskCanvas.width, y * maskCanvas.height));
      context.closePath(); context.fillStyle = '#3b7a5755'; context.fill();
      context.strokeStyle = '#245e3b'; context.lineWidth = 3; context.stroke();
      context.fillStyle = '#ffffff';
      points.forEach(([x, y]) => { context.beginPath(); context.arc(x * maskCanvas.width, y * maskCanvas.height, 5, 0, Math.PI * 2); context.fill(); });
    }
    if (keyboard) {
      const [x, y] = [cursor[0] * maskCanvas.width, cursor[1] * maskCanvas.height];
      context.strokeStyle = '#e46933'; context.lineWidth = 3; context.strokeRect(x - 6, y - 6, 12, 12);
    }
    for (const [x, y, label] of corrections) {
      const px = x * maskCanvas.width, py = y * maskCanvas.height;
      context.fillStyle = label ? '#245e3b' : '#a52a50';
      context.beginPath(); context.arc(px, py, 10, 0, Math.PI * 2); context.fill();
      context.strokeStyle = '#fff'; context.lineWidth = 2;
      context.beginPath(); context.moveTo(px - 5, py); context.lineTo(px + 5, py);
      if (label) { context.moveTo(px, py - 5); context.lineTo(px, py + 5); }
      context.stroke();
    }
    document.getElementById('mask-points').value = JSON.stringify(points);
    document.getElementById('segment-label').textContent = corrections.length ? '보정 적용' : '상품 자동 선택';
    message.textContent = maskInput.value ? '분리 결과를 확인하세요' : mode.value === 'auto'
      ? (corrections.length ? `${corrections.length}/20점 · 보정 적용` : points.length === 4 ? '상품 자동 선택을 누르세요' : '상자의 양 끝을 누르세요')
      : `${points.length}개 점 · 안쪽 보존`;
  }
  function addPoint(point) {
    if (mode.value === 'auto' && tool.value !== 'box') {
      if (points.length !== 4) { message.textContent = '상품 상자를 먼저 표시'; return; }
      if (corrections.length >= 20) { message.textContent = '보정점은 최대 20개'; return; }
      clearAutomaticMask();
      corrections.push([...point, tool.value === 'include' ? 1 : 0]);
      drawMask();
      return;
    }
    clearAutomaticMask(); corrections = [];
    if (mode.value === 'auto') {
      if (points.length !== 1) points = [point];
      else {
        const [a, b] = points[0], [x, y] = point;
        points = [[Math.min(a,x), Math.min(b,y)], [Math.max(a,x), Math.min(b,y)], [Math.max(a,x), Math.max(b,y)], [Math.min(a,x), Math.max(b,y)]];
      }
    } else if (points.length < 80) points.push(point);
    drawMask();
  }
  function ready() {
    maskCanvas.width = Math.min(maskSource.naturalWidth, 960);
    maskCanvas.height = Math.round(maskCanvas.width * maskSource.naturalHeight / maskSource.naturalWidth);
    drawMask();
  }
  maskSource.addEventListener('load', ready);
  if (maskSource.complete && maskSource.naturalWidth) ready();
  maskCanvas.addEventListener('click', event => {
    const box = maskCanvas.getBoundingClientRect(); keyboard = false;
    addPoint([Math.max(0, Math.min(1, (event.clientX - box.left) / box.width)), Math.max(0, Math.min(1, (event.clientY - box.top) / box.height))]);
  });
  maskCanvas.addEventListener('keydown', event => {
    const movement = { ArrowLeft: [-0.02, 0], ArrowRight: [0.02, 0], ArrowUp: [0, -0.02], ArrowDown: [0, 0.02] }[event.key];
    if (movement) { event.preventDefault(); keyboard = true; cursor = cursor.map((value, index) => Math.max(0, Math.min(1, value + movement[index]))); drawMask(); }
    if (event.key === 'Enter') { event.preventDefault(); addPoint([...cursor]); }
    if (event.key === 'Backspace') { event.preventDefault(); undo(); }
  });
  function undo() { clearAutomaticMask(); if (corrections.length) corrections.pop(); else points.pop(); drawMask(); }
  document.getElementById('mask-undo').addEventListener('click', undo);
  document.getElementById('mask-reset').addEventListener('click', () => { clearAutomaticMask(); points = []; corrections = []; tool.value = 'box'; drawMask(); });
  mode.addEventListener('change', () => {
    clearAutomaticMask(); points = []; corrections = []; tool.value = 'box';
    document.getElementById('mask-corrections').hidden = mode.value !== 'auto';
    segmentButton.hidden = mode.value !== 'auto'; drawMask();
  });
  refine.addEventListener('change', () => { clearAutomaticMask(); drawMask(); });
  segmentButton.addEventListener('click', async () => {
    if (mode.value !== 'auto' || points.length !== 4) { message.textContent = '상품 상자를 지정하세요'; return; }
    clearAutomaticMask();
    const version = selectionVersion;
    const form = document.getElementById('image-generation');
    const data = new FormData();
    for (const name of ['csrf', 'revision', 'mask_points']) data.set(name, form.elements[name].value);
    data.set('corrections', JSON.stringify(corrections));
    data.set('refine_edges', refine.checked ? 'yes' : 'no');
    segmentButton.disabled = true; segmentButton.setAttribute('aria-busy', 'true');
    document.getElementById('segment-spinner').hidden = false;
    message.textContent = '상품 분리 중';
    try {
      const response = await fetch(segmentButton.dataset.url, {method:'POST', body:data, headers:{'Accept':'application/json'}});
      const body = await response.json();
      if (version !== selectionVersion) return;
      if (!response.ok) throw new Error(shortMessage(body.detail, '상품 선택 실패'));
      maskInput.value = body.mask_png;
      document.querySelector('#image-generation input[name="confirmed"]').checked = false;
      document.getElementById('mask-cutout-image').src = 'data:image/png;base64,' + body.preview_png;
      cutout.hidden = false;
      message.textContent = '분리 결과를 확인하세요';
    } catch (error) {
      if (version === selectionVersion) { maskInput.value = ''; cutout.hidden = true; message.textContent = shortMessage(error.message, '상품 선택 실패'); }
    } finally {
      segmentButton.disabled = false; segmentButton.removeAttribute('aria-busy');
      document.getElementById('segment-spinner').hidden = true;
    }
  });
}

// 본문·해시태그 수정은 로컬 미리보기만 바꾸며 AI를 다시 호출하지 않는다.
for (const [input, output] of [['caption', 'feed-caption'], ['hashtags', 'feed-hashtags']]) {
  document.getElementById(input)?.addEventListener('input', event => {
    const target = document.getElementById(output);
    if (target) target.textContent = event.target.value;
  });
}

function shortMessage(value, fallback) { return typeof value === 'string' && value.length <= 15 ? value : fallback; }

const progressPanel = document.getElementById('image-progress');
let elapsedTimer, pollTimer, pollStopped = false;
async function submitImageGeneration(form) {
  let rejected = false;
  try {
    const response = await fetch(form.action, {
      method: 'POST', body: new FormData(form), headers: {'Accept': 'application/json'}
    });
    if (!response.ok) {
      rejected = true;
      let message = '이미지 요청 실패';
      try { message = (await response.json()).detail || message; } catch { /* HTML 오류 응답은 표시하지 않는다. */ }
      throw new Error(shortMessage(message, response.status === 402 ? 'AI 예산을 확인하세요' : '이미지 요청 실패'));
    }
    pollStopped = false;
    await pollImageProgress();
  } catch (error) {
    clearInterval(elapsedTimer);
    progressPanel.querySelector('.spinner').hidden = true;
    document.getElementById('image-progress-label').textContent = shortMessage(error.message, '요청 상태 확인 필요');
    document.getElementById('image-progress-estimate').textContent = '요청 상태 확인 필요';
    form.removeAttribute('aria-busy'); delete form.dataset.submitting;
    document.getElementById('image-recheck').hidden = false;
    // 확실한 거절만 버튼을 복구한다. 응답 유실은 상태 확인 전 새 호출을 막는다.
    if (rejected) { try { await refreshImageCandidates(); } catch { /* 상태 확인 버튼으로 복구한다. */ } }
  }
}
function showImageProgress(job) {
  if (!progressPanel) return;
  progressPanel.hidden = false;
  const running = !job || ['queued', 'running'].includes(job.state);
  progressPanel.querySelector('.spinner').hidden = !running;
  const label = document.getElementById('image-progress-label');
  label.textContent = running ? '이미지 생성 중' : (job.state === 'completed' ? '이미지 생성 완료' : shortMessage(job.message, '생성 상태 확인 필요'));
  const start = Date.now() - (job?.elapsed_seconds || 0) * 1000;
  const expected = job?.expected_seconds;
  const estimate = document.getElementById('image-progress-estimate');
  function tick() {
    const elapsed = Math.floor((Date.now() - start) / 1000);
    document.getElementById('image-progress-time').textContent = `${elapsed}초 경과`;
    estimate.textContent = expected
      ? (running && elapsed > expected ? '평소보다 지연 중' : `약 ${expected}초 · 참고용`)
      : '예상 시간 집계 중';
  }
  clearInterval(elapsedTimer); tick();
  if (running) elapsedTimer = setInterval(tick, 1000);
}

async function refreshImageCandidates() {
  // 화면 전체를 다시 열지 않아 작성 중인 본문과 상품 보존 영역을 유지한다.
  const response = await fetch(window.location.href, {cache: 'no-store'});
  if (!response.ok) throw new Error('후보 조회 실패');
  const fresh = new DOMParser().parseFromString(await response.text(), 'text/html');
  const candidates = fresh.getElementById('image-candidates');
  if (!candidates) throw new Error('후보를 확인할 수 없음');
  document.getElementById('image-candidates').replaceWith(candidates);
  const form = document.getElementById('image-generation');
  const latest = fresh.getElementById('image-generation');
  const button = form.querySelector('button[type="submit"]');
  const next = latest.querySelector('button[type="submit"]');
  button.disabled = next.disabled; button.textContent = next.textContent;
  form.querySelector('[name="revision"]').value = latest.querySelector('[name="revision"]').value;
  document.getElementById('image-attempts').textContent = fresh.getElementById('image-attempts').textContent;
  document.getElementById('image-block').textContent = fresh.getElementById('image-block').textContent;
  form.removeAttribute('aria-busy'); delete form.dataset.submitting;
}

async function pollImageProgress() {
  if (!progressPanel || pollStopped) return;
  try {
    const response = await fetch(progressPanel.dataset.url, {cache: 'no-store'});
    if (!response.ok) throw new Error('상태 조회 실패');
    const {job} = await response.json();
    if (!job) {
      await refreshImageCandidates(); pollStopped = true;
      clearInterval(elapsedTimer); progressPanel.querySelector('.spinner').hidden = true;
      document.getElementById('image-progress-label').textContent = '요청 기록 없음';
      document.getElementById('image-recheck').hidden = true;
      return;
    }
    showImageProgress(job);
    if (!['queued', 'running'].includes(job.state)) {
      await refreshImageCandidates();
      pollStopped = true;
      document.getElementById('image-recheck').hidden = true;
      return;
    }
  } catch {
    document.getElementById('image-progress-label').textContent = '상태 다시 확인 중';
  }
  // 초기 화면 갱신 간격이며 이 조회는 새 AI 요청을 만들지 않는다.
  pollTimer = setTimeout(pollImageProgress, 2000);
}
document.getElementById('image-recheck')?.addEventListener('click', () => { pollStopped = false; clearTimeout(pollTimer); pollImageProgress(); });
if (progressPanel && ['queued', 'running'].includes(progressPanel.dataset.state)) pollImageProgress();
window.addEventListener('pagehide', () => { pollStopped = true; clearInterval(elapsedTimer); clearTimeout(pollTimer); });
