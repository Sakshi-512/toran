// Records a voice note (up to 2 minutes) and sends it with the intake form.
// Without MediaRecorder or a microphone, the upload and typing options still work.
(function () {
  "use strict";
  var MAX_MS = 120000;
  var button = document.getElementById("record-button");
  var status = document.getElementById("record-status");
  var form = document.getElementById("intake-form");
  if (!button || !form) return;
  if (!window.MediaRecorder || !navigator.mediaDevices || !navigator.mediaDevices.getUserMedia) {
    document.getElementById("record-unsupported").hidden = false;
    return;
  }
  button.hidden = false;
  var recorder = null, chunks = [], timer = null, started = 0;
  var unsent = null; // a recording that could not be sent yet: kept, never thrown away

  function tick() {
    var left = Math.max(0, Math.ceil((MAX_MS - (Date.now() - started)) / 1000));
    status.textContent = button.dataset.recording + " (" + Math.floor(left / 60) + ":" + String(left % 60).padStart(2, "0") + ")";
    if (left <= 0) stop();
  }
  function stop() {
    if (recorder && recorder.state === "recording") recorder.stop();
    clearInterval(timer);
  }
  function send(blob) {
    var data = new FormData(form);
    data.set("source", "voice");
    data.set("upload", blob, "voice-note" + (blob.type.indexOf("mp4") >= 0 ? ".m4a" : ".webm"));
    if (!form.querySelector("[name=consent]").checked) { form.reportValidity(); return; }
    unsent = blob;
    button.disabled = true;
    button.setAttribute("aria-busy", "true");
    status.textContent = button.dataset.sending;
    fetch(form.action, { method: "POST", body: data, credentials: "same-origin" })
      .then(function (r) { unsent = null; window.location.assign(r.url); })
      .catch(function () {
        // Keep the recording and offer to send the same one again.
        button.disabled = false;
        button.removeAttribute("aria-busy");
        button.textContent = button.dataset.again;
        status.textContent = button.dataset.failed;
      });
  }
  button.addEventListener("click", function () {
    if (unsent && !(recorder && recorder.state === "recording")) { send(unsent); return; }
    if (recorder && recorder.state === "recording") { stop(); return; }
    navigator.mediaDevices.getUserMedia({ audio: true }).then(function (stream) {
      chunks = [];
      recorder = new MediaRecorder(stream);
      recorder.ondataavailable = function (e) { if (e.data.size) chunks.push(e.data); };
      recorder.onstop = function () {
        stream.getTracks().forEach(function (t) { t.stop(); });
        button.textContent = button.dataset.idle;
        send(new Blob(chunks, { type: recorder.mimeType || "audio/webm" }));
      };
      recorder.start();
      started = Date.now();
      button.textContent = button.dataset.recording;
      timer = setInterval(tick, 500);
    }).catch(function () {
      document.getElementById("record-unsupported").hidden = false;
    });
  });
})();
