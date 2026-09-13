// Progressive enhancement only: every page works without JavaScript.
(function () {
  "use strict";

  // Loading overlay, confirmations and double-submit protection for long-running forms.
  document.addEventListener("submit", function (event) {
    var form = event.target;
    var confirmText = form.getAttribute("data-confirm");
    if (confirmText && !window.confirm(confirmText)) {
      event.preventDefault();
      return;
    }
    var loadingText = form.getAttribute("data-loading");
    if (loadingText) {
      var overlay = document.getElementById("loading-overlay");
      var label = document.getElementById("loading-text");
      if (label) { label.textContent = loadingText; }
      if (overlay) { overlay.hidden = false; }
    }
    var buttons = form.querySelectorAll("button[type=submit]");
    window.setTimeout(function () {
      buttons.forEach(function (button) { button.disabled = true; });
    }, 0);
  });

  window.addEventListener("pageshow", function () {
    var overlay = document.getElementById("loading-overlay");
    if (overlay) { overlay.hidden = true; }
    document.querySelectorAll("button[type=submit]").forEach(function (b) { b.disabled = false; });
  });

  // Answer word counter.
  document.querySelectorAll("textarea[data-counter]").forEach(function (area) {
    var target = document.getElementById(area.getAttribute("data-counter"));
    if (!target) { return; }
    var update = function () {
      var words = area.value.trim() ? area.value.trim().split(/\s+/).length : 0;
      target.textContent = words + (words === 1 ? " word" : " words") + (words > 250 ? " · consider tightening" : "");
    };
    area.addEventListener("input", update);
    update();
  });

  // Deck dropzone: drag and drop, chosen-file display, client-side type and size checks.
  // The server validates every upload regardless.
  document.querySelectorAll("[data-dropzone]").forEach(function (zone) {
    var input = zone.querySelector("input[type=file]");
    var title = zone.querySelector("[data-dropzone-title]");
    var sub = zone.querySelector("[data-dropzone-sub]");
    if (!input || !title || !sub) { return; }
    var maxBytes = Number(zone.getAttribute("data-max-bytes") || 0);
    var allowed = (zone.getAttribute("data-accept") || "").split(",").filter(Boolean);
    var originalTitle = title.textContent;
    var originalSub = sub.innerHTML;

    var formatSize = function (bytes) {
      return bytes >= 1048576 ? (bytes / 1048576).toFixed(1) + " MB" : Math.max(1, Math.round(bytes / 1024)) + " KB";
    };

    var show = function (file) {
      zone.classList.remove("invalid", "has-file");
      input.setCustomValidity("");
      if (!file) {
        title.textContent = originalTitle;
        sub.innerHTML = originalSub;
        return;
      }
      var name = file.name.toLowerCase();
      var problem = "";
      if (allowed.length && !allowed.some(function (ext) { return name.endsWith(ext); })) {
        problem = "Unsupported file type. Upload a PDF, PPTX or PPT deck.";
      } else if (maxBytes && file.size > maxBytes) {
        problem = "This file is " + formatSize(file.size) + ", above the " + formatSize(maxBytes) + " limit.";
      }
      title.textContent = file.name;
      if (problem) {
        zone.classList.add("invalid");
        sub.textContent = problem;
        input.setCustomValidity(problem);
      } else {
        zone.classList.add("has-file");
        sub.textContent = formatSize(file.size) + " · ready to analyze · click to change";
      }
    };

    input.addEventListener("change", function () { show(input.files && input.files[0]); });
    ["dragenter", "dragover"].forEach(function (type) {
      zone.addEventListener(type, function (event) { event.preventDefault(); zone.classList.add("dragover"); });
    });
    ["dragleave", "dragend", "drop"].forEach(function (type) {
      zone.addEventListener(type, function (event) { event.preventDefault(); zone.classList.remove("dragover"); });
    });
    zone.addEventListener("drop", function (event) {
      var files = event.dataTransfer && event.dataTransfer.files;
      if (files && files.length) {
        try { input.files = files; } catch (err) { /* older browsers: fall back to the file picker */ }
        show(files[0]);
      }
    });
    show(input.files && input.files[0]);
  });
})();
