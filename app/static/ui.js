/** 폼 중복 클릭·본문 복사·원본 미리보기·키보드 지원 상품 보존 영역을 관리한다. */
document.addEventListener('submit', event => {
    const form = event.target;
    if (!(form instanceof HTMLFormElement) || form.method.toLowerCase() !== 'post') return;
    let points = [];
    if (form.id === 'image-generation') {
      try { points = JSON.parse(document.getElementById('mask-points').value); } catch { /* 아래 검증에서 안내한다. */ }
    }
    if (form.id === 'image-generation' && (!Array.isArray(points) || points.length < 3)) {
      event.preventDefault();
      document.getElementById('mask-message').textContent = '상품을 둘러싸는 점을 세 개 이상 표시해주세요';
      document.getElementById('mask-editor').focus();
      return;
    }
    if (form.id === 'image-generation' && document.getElementById('mask-mode')?.value === 'auto' && !document.getElementById('mask-png').value) {
      event.preventDefault();
      document.getElementById('mask-message').textContent = '상품 자동 선택을 누르고 분리된 상품을 확인해주세요';
      return;
    }
    if (form.dataset.submitting) { event.preventDefault(); return; }
    form.dataset.submitting = 'yes';
    form.setAttribute('aria-busy', 'true');
    if (form.id === 'image-generation') showImageProgress();
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
    document.getElementById('copy-message').textContent = '본문과 해시태그를 복사했습니다';
  } catch {
    caption.focus(); caption.select();
    document.getElementById('copy-message').textContent = '본문을 선택했습니다. 직접 복사해주세요.';
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
    document.getElementById('file-message').textContent = '8MB 이하의 이미지를 업로드 해주세요';
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
  let selectionVersion = 0;
  let points = [], cursor = [0.5, 0.5], keyboard = false;
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
    document.getElementById('mask-points').value = JSON.stringify(points);
    message.textContent = mode.value === 'auto'
      ? (points.length === 4 ? '영역을 지정했어요. 상품 자동 선택을 눌러주세요.' : '상품 전체가 들어오는 사각형의 양 끝을 눌러주세요.')
      : `${points.length}개 점 · 선택한 안쪽을 보존합니다. 경계를 확인해주세요.`;
  }
  function addPoint(point) {
    clearAutomaticMask();
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
    if (event.key === 'Backspace') { event.preventDefault(); clearAutomaticMask(); points.pop(); drawMask(); }
  });
  document.getElementById('mask-undo').addEventListener('click', () => { clearAutomaticMask(); points.pop(); drawMask(); });
  document.getElementById('mask-reset').addEventListener('click', () => { clearAutomaticMask(); points = []; drawMask(); });
  mode.addEventListener('change', () => { clearAutomaticMask(); points = []; drawMask(); });
  segmentButton.addEventListener('click', async () => {
    if (mode.value !== 'auto' || points.length !== 4) { message.textContent = '자동 선택 모드에서 상품 전체가 들어오는 상자를 먼저 지정해주세요'; return; }
    const version = selectionVersion;
    const form = document.getElementById('image-generation');
    const data = new FormData();
    for (const name of ['csrf', 'revision', 'mask_points']) data.set(name, form.elements[name].value);
    segmentButton.disabled = true; segmentButton.setAttribute('aria-busy', 'true');
    document.getElementById('segment-spinner').hidden = false;
    message.textContent = '상품을 분리하고 있어요. 처음에는 모델을 불러오는 시간이 필요해요.';
    try {
      const response = await fetch(segmentButton.dataset.url, {method:'POST', body:data, headers:{'Accept':'application/json'}});
      const body = await response.json();
      if (version !== selectionVersion) return;
      if (!response.ok) throw new Error(body.detail || '상품을 선택하지 못했습니다');
      maskInput.value = body.mask_png;
      document.querySelector('#image-generation input[name="confirmed"]').checked = false;
      document.getElementById('mask-cutout-image').src = 'data:image/png;base64,' + body.preview_png;
      cutout.hidden = false;
      message.textContent = '자동으로 분리했어요. 아래 미리보기에서 상품 전체와 경계를 확인한 뒤 이미지를 생성해주세요.';
    } catch (error) {
      if (version === selectionVersion) { maskInput.value = ''; cutout.hidden = true; message.textContent = error.message + ' 영역을 다시 지정하거나 수동 경계를 사용해주세요.'; }
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

const progressPanel = document.getElementById('image-progress');
let elapsedTimer, pollTimer, pollStopped = false;
async function submitImageGeneration(form) {
  try {
    const response = await fetch(form.action, {
      method: 'POST', body: new FormData(form), headers: {'Accept': 'application/json'}
    });
    if (!response.ok) {
      let message = '이미지 요청을 시작하지 못했어요. 설정과 연결 상태를 확인해주세요.';
      try { message = (await response.json()).detail || message; } catch { /* HTML 오류 응답은 표시하지 않는다. */ }
      throw new Error(message);
    }
    pollStopped = false;
    await pollImageProgress();
  } catch (error) {
    clearInterval(elapsedTimer);
    progressPanel.querySelector('.spinner').hidden = true;
    document.getElementById('image-progress-label').textContent = error.message;
    document.getElementById('image-progress-estimate').textContent = '새로고침 후 요청 상태를 확인해주세요. 자동으로 재요청하지 않아요.';
    form.removeAttribute('aria-busy'); delete form.dataset.submitting;
    // 응답 유실은 이미 접수됐을 수 있어 화면에서 즉시 재호출하지 않는다.
  }
}
function showImageProgress(job) {
  if (!progressPanel) return;
  progressPanel.hidden = false;
  const running = !job || ['queued', 'running'].includes(job.state);
  progressPanel.querySelector('.spinner').hidden = !running;
  const label = document.getElementById('image-progress-label');
  label.textContent = running ? '이미지를 만들고 있어요' : (job.state === 'completed' ? '이미지 생성이 완료됐어요' : job.message);
  const start = Date.now() - (job?.elapsed_seconds || 0) * 1000;
  const expected = job?.expected_seconds;
  const estimate = document.getElementById('image-progress-estimate');
  function tick() {
    const elapsed = Math.floor((Date.now() - start) / 1000);
    document.getElementById('image-progress-time').textContent = `${elapsed}초 경과`;
    estimate.textContent = expected
      ? `같은 설정의 이전 ${job.sample_count}건 기준 약 ${expected}초 · ${running && elapsed > expected ? '이전보다 오래 걸리고 있어요' : '참고용 예상 시간이에요'}`
      : '완료 이력이 쌓이면 예상 시간을 안내해요. 생성 시간은 요청마다 달라요.';
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
  form.removeAttribute('aria-busy'); delete form.dataset.submitting;
}

async function pollImageProgress() {
  if (!progressPanel || pollStopped) return;
  try {
    const response = await fetch(progressPanel.dataset.url, {cache: 'no-store'});
    if (!response.ok) throw new Error('상태 조회 실패');
    const {job} = await response.json();
    if (!job) { pollStopped = true; return; }
    showImageProgress(job);
    if (!['queued', 'running'].includes(job.state)) {
      await refreshImageCandidates();
      pollStopped = true;
      return;
    }
  } catch {
    document.getElementById('image-progress-label').textContent = '상태를 다시 확인하고 있어요. 생성 요청은 반복하지 않아요.';
  }
  // 초기 화면 갱신 간격이며 이 조회는 새 AI 요청을 만들지 않는다.
  pollTimer = setTimeout(pollImageProgress, 2000);
}
if (progressPanel && ['queued', 'running'].includes(progressPanel.dataset.state)) pollImageProgress();
window.addEventListener('pagehide', () => { pollStopped = true; clearInterval(elapsedTimer); clearTimeout(pollTimer); });
