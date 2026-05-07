function focusableElements(container) {
  return [...container.querySelectorAll('button, [href], input, select, textarea, [tabindex]:not([tabindex="-1"])')].filter(
    (element) => !element.hasAttribute("disabled") && element.getAttribute("aria-hidden") !== "true"
  );
}

export function createModalController({ modal, dialog, backdrop, closeButton, onClose: initialOnClose = null }) {
  let previousFocus = null;
  let onClose = initialOnClose;
  let closeTimer = null;
  const ANIMATION_MS = 180;

  const trapFocus = (event) => {
    if (event.key !== "Tab" || modal.classList.contains("hidden")) {
      return;
    }
    const focusables = focusableElements(dialog);
    if (!focusables.length) {
      event.preventDefault();
      dialog.focus();
      return;
    }
    const first = focusables[0];
    const last = focusables[focusables.length - 1];
    if (event.shiftKey && document.activeElement === first) {
      event.preventDefault();
      last.focus();
    } else if (!event.shiftKey && document.activeElement === last) {
      event.preventDefault();
      first.focus();
    }
  };

  const handleKeydown = (event) => {
    if (modal.classList.contains("hidden")) {
      return;
    }
    if (event.key === "Escape") {
      event.preventDefault();
      close();
      return;
    }
    trapFocus(event);
  };

  const open = () => {
    if (closeTimer) {
      clearTimeout(closeTimer);
      closeTimer = null;
    }
    previousFocus = document.activeElement instanceof HTMLElement ? document.activeElement : null;
    modal.classList.remove("hidden");
    modal.setAttribute("aria-hidden", "false");
    document.addEventListener("keydown", handleKeydown);
    window.requestAnimationFrame(() => {
      modal.classList.add("is-open");
      const focusables = focusableElements(dialog);
      (focusables[0] || dialog).focus();
    });
  };

  const close = () => {
    modal.classList.remove("is-open");
    document.removeEventListener("keydown", handleKeydown);
    closeTimer = window.setTimeout(() => {
      modal.classList.add("hidden");
      modal.setAttribute("aria-hidden", "true");
      previousFocus?.focus?.();
      onClose?.();
      closeTimer = null;
    }, ANIMATION_MS);
  };

  backdrop?.addEventListener("click", close);
  closeButton?.addEventListener("click", close);
  dialog.setAttribute("tabindex", "-1");

  const setOnClose = (handler) => {
    onClose = handler;
  };

  return { open, close, setOnClose };
}
