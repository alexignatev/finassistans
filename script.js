// GSAP анимации
gsap.registerPlugin();

// Анимация при загрузке страницы
window.addEventListener('load', () => {
    // Анимация навигации
    gsap.from('.navbar', {
        duration: 1,
        y: -100,
        opacity: 0,
        ease: 'power3.out'
    });

    // Анимация героя
    gsap.to('.hero-title', {
        duration: 1,
        opacity: 1,
        y: 0,
        delay: 0.3,
        ease: 'power3.out'
    });

    gsap.to('.hero-subtitle', {
        duration: 1,
        opacity: 1,
        y: 0,
        delay: 0.5,
        ease: 'power3.out'
    });

    // Анимация карточек
    gsap.to('.card', {
        duration: 0.8,
        opacity: 1,
        y: 0,
        stagger: 0.2,
        delay: 0.8,
        ease: 'power3.out'
    });
});

// Мобильное меню теперь обрабатывается в loader.js

// Плавная прокрутка для якорных ссылок
document.querySelectorAll('a[href^="#"]').forEach(anchor => {
    anchor.addEventListener('click', function (e) {
        e.preventDefault();
        const target = document.querySelector(this.getAttribute('href'));
        if (target) {
            target.scrollIntoView({
                behavior: 'smooth',
                block: 'start'
            });
        }
    });
});

// Анимация при скролле
const observerOptions = {
    threshold: 0.1,
    rootMargin: '0px 0px -50px 0px'
};

const observer = new IntersectionObserver((entries) => {
    entries.forEach(entry => {
        if (entry.isIntersecting) {
            gsap.to(entry.target, {
                duration: 0.8,
                opacity: 1,
                y: 0,
                ease: 'power3.out'
            });
        }
    });
}, observerOptions);

// Наблюдение за элементами
document.addEventListener('DOMContentLoaded', () => {
    const animatedElements = document.querySelectorAll('.content-block, .card');
    animatedElements.forEach(el => {
        observer.observe(el);
    });
});

// Эффект параллакса для фона был удален, так как он приводил к наложению контента.


