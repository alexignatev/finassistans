// Функция для загрузки компонентов
function loadComponent(elementId, componentPath) {
    const xhr = new XMLHttpRequest();
    xhr.open('GET', componentPath, true);
    
    xhr.onload = function() {
        if (xhr.status === 200) {
            document.getElementById(elementId).innerHTML = xhr.responseText;
            
            // Если загружается header, инициализируем мобильное меню
            if (elementId === 'header') {
                initMobileMenu();
            }
        } else {
            console.error('Ошибка загрузки компонента:', xhr.status);
            insertFallbackComponent(elementId);
        }
    };
    
    xhr.onerror = function() {
        console.error('Ошибка сети при загрузке компонента');
        insertFallbackComponent(elementId);
    };
    
    xhr.send();
}

// Fallback компоненты
function insertFallbackComponent(elementId) {
    if (elementId === 'header') {
        const currentPath = window.location.pathname;
        const depth = (currentPath.match(/\//g) || []).length - 1;
        const prefix = depth > 0 ? '../'.repeat(depth) : '';
        
        const headerHTML = `
            <nav class="navbar">
                <div class="nav-container">
                    <div class="logo">
                        <a href="${prefix}index.html">ФА</a>
                    </div>
                    <ul class="nav-menu">
                        <li><a href="${prefix}situations.html">Ситуации</a></li>
                        <li><a href="${prefix}work.html">Как мы работаем</a></li>
                        <li><a href="${prefix}tools.html">Наши инструменты</a></li>
                        <li><a href="${prefix}tech.html">Технологический контур</a></li>
                        <li><a href="${prefix}team.html">Команда</a></li>
                        <li><a href="${prefix}industries.html">Отрасли</a></li>
                        <li><a href="${prefix}contacts.html">Контакты</a></li>
                    </ul>
                    <div class="hamburger">
                        <span></span>
                        <span></span>
                        <span></span>
                    </div>
                </div>
            </nav>
        `;
        document.getElementById(elementId).innerHTML = headerHTML;
        initMobileMenu();
    } else if (elementId === 'footer') {
        const footerHTML = `
            <footer>
                <div class="container">
                    <p>&copy; 2024. Все права защищены.</p>
                </div>
            </footer>
        `;
        document.getElementById(elementId).innerHTML = footerHTML;
    }
}

// Инициализация мобильного меню
function initMobileMenu() {
    const hamburger = document.querySelector('.hamburger');
    const navMenu = document.querySelector('.nav-menu');
    
    if (hamburger && navMenu) {
        hamburger.addEventListener('click', () => {
            hamburger.classList.toggle('active');
            navMenu.classList.toggle('active');
        });

        // Закрытие меню при клике на ссылку
        document.querySelectorAll('.nav-menu a').forEach(link => {
            link.addEventListener('click', () => {
                hamburger.classList.remove('active');
                navMenu.classList.remove('active');
            });
        });
    }
}

// Загрузка компонентов при загрузке страницы
document.addEventListener('DOMContentLoaded', () => {
    const currentPath = window.location.pathname;
    const depth = (currentPath.match(/\//g) || []).length - 1;
    const prefix = depth > 0 ? '../'.repeat(depth) : '';
    
    // Показываем индикатор загрузки
    showLoadingIndicator();
    
    // Загружаем компоненты
    loadComponent('header', prefix + 'components/header.html');
    loadComponent('footer', prefix + 'components/footer.html');
    
    // Корректировка ссылок после загрузки header
    setTimeout(() => {
        hideLoadingIndicator();
    }, 200);
});

// Индикатор загрузки
function showLoadingIndicator() {
    const header = document.getElementById('header');
    const footer = document.getElementById('footer');
    
    if (header) {
        header.innerHTML = '<div style="text-align: center; padding: 20px; color: #666;">Загрузка...</div>';
    }
    if (footer) {
        footer.innerHTML = '<div style="text-align: center; padding: 20px; color: #666;">Загрузка...</div>';
    }
}

function hideLoadingIndicator() {
    // Индикатор скроется автоматически при загрузке компонентов
}
