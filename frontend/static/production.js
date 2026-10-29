/** API 시간 대신 실제 화면 준비까지의 시간을 기록한다. 서버의 비용 원장과 구분한다. */
const productionContext = document.getElementById('production-context');
const previewTimingKey = 'posting:preview-timing', imageTimingKey = 'posting:image-timing', creationTimingKey = 'posting:creation-timing';
function clockNow() { return performance.timeOrigin + performance.now(); }
function timingId() { return crypto.randomUUID().replaceAll('-', ''); }
function readTiming(key) { try { return JSON.parse(sessionStorage.getItem(key)); } catch { return null; } }

document.addEventListener('submit', event => {
  if (event.defaultPrevented) return;
  const form = event.target;
  if (form.method?.toLowerCase() !== 'post') return;
  const path = new URL(form.action).pathname;
  if (path === '/posts') {
    const value = {id:timingId(), action:'initial_preview', start:clockNow()};
    let field = form.querySelector('[name="measurement_id"]');
    if (!field) { field = document.createElement('input'); field.type = 'hidden'; field.name = 'measurement_id'; form.append(field); }
    field.value = value.id;
    sessionStorage.setItem(previewTimingKey, JSON.stringify(value));
    sessionStorage.setItem(creationTimingKey, JSON.stringify(value));
  } else if (productionContext && /\/posts\/[^/]+\/(edit|generate|style|images\/select)$/.test(path)) {
    sessionStorage.setItem(previewTimingKey, JSON.stringify({id:timingId(), action:'edit_preview', post:productionContext.dataset.post, revision:Number(productionContext.dataset.revision), start:clockNow()}));
  } else if (form.id === 'image-generation' && form.elements.confirmed.checked) {
    sessionStorage.setItem(imageTimingKey, JSON.stringify({id:timingId(), action:'image_candidate', post:productionContext.dataset.post, previous:document.getElementById('image-progress').dataset.jobId, start:clockNow()}));
  }
});

async function imageDecoded(image) {
  if (!image) throw new Error('미리보기를 찾을 수 없어요');
  await image.decode();
  await new Promise(resolve => requestAnimationFrame(() => requestAnimationFrame(resolve)));
}

async function sendTiming(value, imageId='') {
  const data = new FormData();
  for (const [key, item] of Object.entries({csrf:document.querySelector('[name="csrf"]').value,
    id:value.id, action:value.action, elapsed_ms:value.elapsed, revision:value.resultRevision ?? productionContext.dataset.revision, image_id:imageId})) data.set(key, item);
  const response = await fetch(`/posts/${productionContext.dataset.post}/timings`, {method:'POST', body:data, headers:{'Accept':'application/json'}});
  if (!response.ok) throw new Error('시간 기록 실패');
  document.getElementById('measurement-status').textContent = `화면 준비 ${(value.elapsed / 1000).toFixed(2)}초 · 기록 완료`;
}

async function recordPreviewReady() {
  if (!productionContext) return;
  const pending = readTiming(previewTimingKey);
  if (!pending || (pending.action === 'initial_preview' ? pending.id !== productionContext.dataset.creation : pending.post !== productionContext.dataset.post)) return;
  if (pending.action === 'edit_preview' && Number(productionContext.dataset.revision) <= pending.revision) { sessionStorage.removeItem(previewTimingKey); return; }
  try {
    await imageDecoded(document.querySelector('.post-preview'));
    pending.elapsed ??= Math.max(0, clockNow() - pending.start);
    pending.resultRevision ??= Number(productionContext.dataset.revision);
    sessionStorage.setItem(previewTimingKey, JSON.stringify(pending));
    await sendTiming(pending);
    sessionStorage.removeItem(previewTimingKey);
  } catch { document.getElementById('measurement-status').textContent = '화면 준비 시간을 기록하지 못했어요'; }
}

async function recordCandidateReady(job) {
  if (!productionContext) return;
  const pending = readTiming(imageTimingKey);
  if (!pending || pending.post !== productionContext.dataset.post || pending.previous === job.id) return;
  try {
    await imageDecoded(document.querySelector(`[data-image-id="${job.id}"] img`));
    pending.elapsed ??= Math.max(0, clockNow() - pending.start);
    pending.resultRevision ??= Number(productionContext.dataset.revision);
    sessionStorage.setItem(imageTimingKey, JSON.stringify(pending));
    await sendTiming(pending, job.id);
    const initial = readTiming(creationTimingKey);
    if (initial && initial.id === productionContext.dataset.creation) {
      initial.action = 'creation_to_ai_candidate';
      initial.elapsed ??= Math.max(0, clockNow() - initial.start);
      initial.totalId ??= timingId();
      initial.resultRevision ??= Number(productionContext.dataset.revision);
      sessionStorage.setItem(creationTimingKey, JSON.stringify(initial));
      // 최초 미리보기와 다른 측정값이며 사용자 조작 시간도 포함한다.
      await sendTiming({...initial, id:initial.totalId}, job.id);
      sessionStorage.removeItem(creationTimingKey);
    }
    sessionStorage.removeItem(imageTimingKey);
  } catch { document.getElementById('measurement-status').textContent = '이미지 후보 준비 시간을 기록하지 못했어요'; }
}
recordPreviewReady();
