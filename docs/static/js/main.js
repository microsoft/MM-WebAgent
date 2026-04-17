// MM-WebAgent project page — interactions

// Mobile nav
(function () {
  const toggle = document.getElementById('nav-toggle');
  const mobileNav = document.getElementById('mobile-nav');
  if (!toggle || !mobileNav) return;
  toggle.addEventListener('click', () => {
    const isOpen = mobileNav.classList.toggle('open');
    toggle.setAttribute('aria-expanded', String(isOpen));
  });
  mobileNav.querySelectorAll('a').forEach(link => {
    link.addEventListener('click', () => {
      mobileNav.classList.remove('open');
      toggle.setAttribute('aria-expanded', 'false');
    });
  });
})();

// Reveal-on-scroll
(function () {
  const els = document.querySelectorAll('.reveal');
  if (!('IntersectionObserver' in window)) {
    els.forEach(e => e.classList.add('in'));
    return;
  }
  const io = new IntersectionObserver((entries) => {
    entries.forEach(entry => {
      if (entry.isIntersecting) {
        entry.target.classList.add('in');
        io.unobserve(entry.target);
      }
    });
  }, { threshold: 0.12, rootMargin: '0px 0px -40px 0px' });
  els.forEach(e => io.observe(e));
})();

// Gallery toggle
(function () {
  const btns = document.querySelectorAll('.gbtn');
  const imgs = document.querySelectorAll('.gallery-img');
  btns.forEach(btn => {
    btn.addEventListener('click', () => {
      const target = btn.dataset.target;
      btns.forEach(b => b.classList.toggle('active', b === btn));
      imgs.forEach(img => img.classList.toggle('active', img.id === target));
    });
  });
})();

// BibTeX copy
(function () {
  const btn = document.getElementById('copy-bib');
  const bib = document.getElementById('bib-content');
  if (!btn || !bib) return;
  btn.addEventListener('click', async () => {
    try {
      await navigator.clipboard.writeText(bib.textContent.trim());
      btn.classList.add('copied');
      const original = btn.innerHTML;
      btn.innerHTML = '<i class="fa-solid fa-check"></i> Copied';
      setTimeout(() => {
        btn.classList.remove('copied');
        btn.innerHTML = original;
      }, 2000);
    } catch (e) {
      console.error('copy failed', e);
    }
  });
})();

// Active nav link based on scroll position
(function () {
  const links = document.querySelectorAll('.nav-link');
  const map = new Map();
  links.forEach(link => {
    const href = link.getAttribute('href');
    if (!href || !href.startsWith('#')) return;
    const sec = document.getElementById(href.slice(1));
    if (!sec) return;
    const arr = map.get(sec) || [];
    arr.push(link);
    map.set(sec, arr);
  });
  if (!('IntersectionObserver' in window) || map.size === 0) return;
  const io = new IntersectionObserver((entries) => {
    entries.forEach(entry => {
      const group = map.get(entry.target);
      if (!group) return;
      if (entry.isIntersecting) {
        links.forEach(l => l.classList.remove('is-active'));
        group.forEach(link => link.classList.add('is-active'));
      }
    });
  }, { rootMargin: '-40% 0px -55% 0px' });
  map.forEach((_, sec) => io.observe(sec));
})();
