from flask import Blueprint, render_template, session, redirect, url_for

main_bp = Blueprint('main', __name__)


@main_bp.route('/')
def home():
    return render_template('home.html')


@main_bp.route('/logout')
def logout():
    session.clear()
    return redirect(url_for('main.home'))
