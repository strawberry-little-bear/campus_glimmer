document.addEventListener('DOMContentLoaded', function () {
    document.querySelectorAll('input[type="file"]').forEach(function (input) {
        input.addEventListener('change', function () {
            const preview = this.closest('.upload-card')?.querySelector('img') || document.getElementById('avatar-preview');
            if (!preview || !this.files || !this.files[0]) return;
            const reader = new FileReader();
            reader.onload = function (event) { preview.src = event.target.result; preview.style.display = 'block'; };
            reader.readAsDataURL(this.files[0]);
        });
    });

    document.querySelectorAll('.confirm-delete').forEach(function (button) {
        button.addEventListener('click', function (event) {
            if (!window.confirm('确定删除这件商品吗？此操作无法撤销。')) event.preventDefault();
        });
    });

    const chat = document.querySelector('.chat-messages');
    if (chat) chat.scrollTop = chat.scrollHeight;
});
