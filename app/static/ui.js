/** 중앙 알림·폼 중복 방지·사진 미리보기·자동 이미지 작업 상태를 관리한다. */
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

const noticeDialog = document.getElementById('notice-dialog');
const confirmedForms = new WeakSet();
function showNotice(message, {confirm=false, title='확인해주세요'} = {}) {
  if (noticeDialog.open) return new Promise(resolve => {
    noticeDialog.addEventListener('close', () => resolve(showNotice(message, {confirm, title})), {once:true});
  });
  document.getElementById('notice-title').textContent = title;
  document.getElementById('notice-message').textContent = typeof message === 'string' ? message : '다시 확인해주세요.';
  const cancel = document.getElementById('notice-cancel'), ok = document.getElementById('notice-ok');
  cancel.hidden = !confirm;
  ok.textContent = confirm ? '삭제' : '확인';
  noticeDialog.setAttribute('role', confirm ? 'dialog' : 'alertdialog');
  noticeDialog.returnValue = '';
  const result = new Promise(resolve => noticeDialog.addEventListener('close', () => resolve(noticeDialog.returnValue === 'confirm'), {once:true}));
  noticeDialog.showModal();
  (confirm ? cancel : ok).focus();
  return result;
}
document.getElementById('notice-ok').addEventListener('click', () => noticeDialog.close('confirm'));
document.getElementById('notice-cancel').addEventListener('click', () => noticeDialog.close('cancel'));
noticeDialog.addEventListener('cancel', () => { noticeDialog.returnValue = 'cancel'; });
const serverError = document.getElementById('server-error');
if (serverError) showNotice(serverError.textContent, {title:'요청 확인 필요'});
const pageWarning = document.getElementById('page-warning');
if (pageWarning) showNotice(pageWarning.textContent);
document.addEventListener('invalid', event => {
  event.preventDefault();
  if (noticeDialog.open) return;
  const field = event.target;
  showNotice(field.validationMessage, {title:'입력을 확인해주세요'}).then(() => field.focus());
}, true);
document.getElementById('image-block-info')?.addEventListener('click', () => {
  showNotice(document.getElementById('image-block').textContent, {title:'이미지 생성 안내'});
});

for (const button of document.querySelectorAll('[data-ai-setup]')) {
  button.addEventListener('click', () => {
    showNotice(document.getElementById(button.dataset.aiSetup).textContent, {title:button.dataset.title});
  });
}
const shopForm = document.getElementById('shop-settings');
if (shopForm) {
  const draftKey = 'posting-style-draft:' + shopForm.dataset.owner;
  shopForm.addEventListener('submit', async event => {
    const button = event.submitter;
    if (!button?.hasAttribute('formaction')) { sessionStorage.removeItem(draftKey); return; }
    event.preventDefault();
    if (shopForm.dataset.filling) return;
    shopForm.dataset.filling = 'yes'; button.disabled = true;
    const status = document.getElementById('style-fill-status');
    status.textContent = '기존 게시물에서 빈칸을 확인하고 있어요';
    const data = new FormData(shopForm); data.set('reference_source', button.value);
    try {
      const response = await fetch(button.formAction, {method:'POST', body:data, headers:{'Accept':'application/json'}});
      const result = await response.json();
      if (result.connect_url) {
        sessionStorage.setItem(draftKey, JSON.stringify([...data].filter(([key]) => key !== 'csrf')));
        window.location.assign(result.connect_url); return;
      }
      if (!response.ok) throw new Error(result.detail || '스타일 제안을 만들지 못했어요');
      if (result.content) {
        // 요청 후 사용자가 새로 입력한 값도 보존한다.
        if (!shopForm.elements.style.value.trim()) shopForm.elements.style.value = result.content.summary;
        for (const [key, value] of Object.entries(result.content.attributes)) {
          const field = shopForm.elements['style_' + key];
          if (!field.value.trim()) field.value = value;
        }
        shopForm.elements.proposal_version.value = result.proposal_version;
        document.getElementById('style-fill-evidence').textContent = [...result.content.evidence, ...result.content.uncertainties].join(' / ');
      }
      status.textContent = result.message;
    } catch (error) {
      status.textContent = '입력한 내용은 유지했어요';
      showNotice(error.message, {title:'스타일 제안 확인'});
    } finally { delete shopForm.dataset.filling; button.disabled = false; }
  });
  const pending = sessionStorage.getItem(draftKey);
  if (pending && shopForm.dataset.connected === 'yes') {
    try {
      const values = new FormData();
      for (const [key, value] of JSON.parse(pending)) values.append(key, value);
      if (values.get('profile_version') === shopForm.elements.profile_version.value) {
        for (const [key, value] of values) {
          const field = shopForm.elements.namedItem(key);
          if (field && !['locked_fields', 'reference_source'].includes(key)) field.value = value;
        }
        for (const field of shopForm.querySelectorAll('[name="locked_fields"]')) field.checked = values.getAll('locked_fields').includes(field.value);
        sessionStorage.removeItem(draftKey);
        shopForm.requestSubmit(shopForm.querySelector('[value="instagram"]'));
      } else showNotice('저장된 스타일이 변경되어 연결 전 임시 입력을 자동 적용하지 않았어요.');
    } catch { showNotice('연결 전 입력을 복원하지 못했어요. 저장된 스타일을 확인해주세요.'); }
  }
}

document.addEventListener('submit', event => {
    if (event.defaultPrevented) return;
    const form = event.target;
    if (!(form instanceof HTMLFormElement) || form.method.toLowerCase() !== 'post') return;
    if (form.dataset.submitting) { event.preventDefault(); return; }
    if (form.dataset.confirm && !confirmedForms.has(form)) {
      event.preventDefault();
      if (form.dataset.confirming) return;
      form.dataset.confirming = 'yes';
      const submitter = event.submitter;
      showNotice(form.dataset.confirm, {confirm:true, title:'삭제 확인'}).then(accepted => {
        delete form.dataset.confirming;
        if (accepted) { confirmedForms.add(form); if (submitter) form.requestSubmit(submitter); else form.requestSubmit(); }
      });
      return;
    }
    confirmedForms.delete(form);
    if (form.id === 'image-generation' && !form.elements.confirmed.checked) {
      event.preventDefault();
      document.getElementById('image-block').textContent = '생성 동의가 필요해요';
      showNotice('이미지 생성 1회에 동의해주세요.').then(() => form.elements.confirmed.focus());
      return;
    }
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
  const text = captionParts(caption.value, document.getElementById('hashtags').value).filter(Boolean).join('\n\n');
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
    showNotice('사진은 8MB 이하로 올려주세요.');
    return;
  }
  document.getElementById('file-message').textContent = file.name;
  photoUrl = URL.createObjectURL(file); image.src = photoUrl; image.hidden = false;
});

// 입력 중인 결과도 저장·실제 게시와 같은 태그 배치를 보여준다.
function updateCaptionPreview() {
  const caption = document.getElementById('caption'), tags = document.getElementById('hashtags');
  if (!caption || !tags) return;
  const parts = captionParts(caption.value, tags.value);
  document.getElementById('feed-caption').textContent = parts[0];
  document.getElementById('feed-hashtags').textContent = parts[1];
}
for (const id of ['caption', 'hashtags']) document.getElementById(id)?.addEventListener('input', updateCaptionPreview);
updateCaptionPreview();

function shortMessage(value, fallback) { return typeof value === 'string' && value.length <= 15 ? value : fallback; }

const progressPanel = document.getElementById('image-progress');
let elapsedTimer, pollTimer, pollStopped = false;
let notifyImageErrors = Boolean(progressPanel && ['queued', 'running'].includes(progressPanel.dataset.state));
let pollErrorShown = false;
async function submitImageGeneration(form) {
  let rejected = false;
  try {
    const response = await fetch(form.action, {
      method: 'POST', body: new FormData(form), headers: {'Accept': 'application/json'}
    });
    if (!response.ok) {
      rejected = true;
      let message = '이미지 요청 실패';
      try { const detail = (await response.json()).detail; if (typeof detail === 'string') message = detail; } catch { /* HTML 오류 응답은 표시하지 않는다. */ }
      throw new Error(message);
    }
    notifyImageErrors = true; pollErrorShown = false; pollStopped = false;
    await pollImageProgress();
  } catch (error) {
    clearInterval(elapsedTimer);
    progressPanel.querySelector('.spinner').hidden = true;
    document.getElementById('image-progress-label').textContent = shortMessage(error.message, '요청 상태 확인 필요');
    document.getElementById('image-progress-estimate').textContent = '요청 상태 확인 필요';
    form.removeAttribute('aria-busy'); delete form.dataset.submitting;
    document.getElementById('image-recheck').hidden = false;
    showNotice(rejected ? error.message : '연결이 끊겨 결과를 확인하지 못했습니다. 요청 상태를 확인한 뒤 진행해주세요.', {title:'이미지 요청 확인'});
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
  // 화면 전체를 다시 열지 않아 작성 중인 본문을 유지한다.
  const response = await fetch(window.location.href, {cache: 'no-store'});
  if (!response.ok) throw new Error('후보 조회 실패');
  const fresh = new DOMParser().parseFromString(await response.text(), 'text/html');
  const candidates = fresh.getElementById('image-candidates');
  if (!candidates) throw new Error('후보를 확인할 수 없음');
  document.getElementById('image-candidates').replaceWith(candidates);
  const form = document.getElementById('image-generation');
  const latest = fresh.getElementById('image-generation');
  progressPanel.dataset.jobId = fresh.getElementById('image-progress').dataset.jobId;
  const button = form.querySelector('button[type="submit"]');
  const next = latest.querySelector('button[type="submit"]');
  button.disabled = next.disabled; button.textContent = next.textContent;
  form.querySelector('[name="revision"]').value = latest.querySelector('[name="revision"]').value;
  document.getElementById('image-attempts').textContent = fresh.getElementById('image-attempts').textContent;
  document.getElementById('image-block').textContent = fresh.getElementById('image-block').textContent;
  document.getElementById('image-block-info').hidden = !fresh.getElementById('image-block').textContent.trim();
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
      if (job.state === 'completed') await recordCandidateReady(job);
      pollStopped = true;
      document.getElementById('image-recheck').hidden = true;
      if (notifyImageErrors && job.state !== 'completed') {
        notifyImageErrors = false;
        showNotice(job.message || '이미지를 생성하지 못했습니다. 연결 상태와 예산을 확인해주세요.', {title:'이미지 생성 확인'});
      }
      return;
    }
  } catch {
    document.getElementById('image-progress-label').textContent = '상태 다시 확인 중';
    if (!pollErrorShown) {
      pollErrorShown = true;
      showNotice('상태를 확인하지 못해 다시 조회 중입니다. 같은 이미지를 중복 요청하지 마세요.', {title:'연결 확인 필요'});
    }
  }
  // 초기 화면 갱신 간격이며 이 조회는 새 AI 요청을 만들지 않는다.
  pollTimer = setTimeout(pollImageProgress, 2000);
}
document.getElementById('image-recheck')?.addEventListener('click', () => { pollStopped = false; clearTimeout(pollTimer); pollImageProgress(); });
if (progressPanel && ['queued', 'running'].includes(progressPanel.dataset.state)) pollImageProgress();
window.addEventListener('pagehide', () => { pollStopped = true; clearInterval(elapsedTimer); clearTimeout(pollTimer); });
