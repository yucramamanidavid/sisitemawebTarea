from flask import Flask, render_template, request, redirect, url_for, flash
import mysql.connector
from datetime import datetime
import os

app = Flask(__name__)
app.secret_key = 'tu_clave_secreta_aqui'

# Configuración de la base de datos
def get_db_connection():
    try:
        conn = mysql.connector.connect(
            host='3.95.201.54',
            user='root',
            password='',
            database='gestion_tareas',
            charset='utf8mb4',
            collation='utf8mb4_unicode_ci'
        )
        return conn
    except mysql.connector.Error as err:
        print(f"Error de conexión: {err}")
        return None

# Crear tabla si no existe
def crear_tabla():
    conn = get_db_connection()
    if conn:
        try:
            cursor = conn.cursor()
            cursor.execute('''
                CREATE TABLE IF NOT EXISTS tareas (
                    id INT AUTO_INCREMENT PRIMARY KEY,
                    titulo VARCHAR(255) NOT NULL,
                    descripcion TEXT,
                    estado ENUM('pendiente', 'en_progreso', 'completada') DEFAULT 'pendiente',
                    fecha_creacion TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    fecha_vencimiento DATE
                )
            ''')
            conn.commit()
            print("✅ Tabla 'tareas' creada/verificada correctamente")
        except mysql.connector.Error as err:
            print(f"❌ Error al crear tabla: {err}")
        finally:
            cursor.close()
            conn.close()

# Verificar e inicializar la base de datos al iniciar
def inicializar_app():
    print("🚀 Inicializando aplicación...")
    crear_tabla()

# Llamar a la inicialización
inicializar_app()

@app.route('/')
def index():
    estado_filtro = request.args.get('estado')
    
    conn = get_db_connection()
    if not conn:
        flash('Error de conexión a la base de datos', 'error')
        return render_template('index.html', tareas=[])
    
    try:
        cursor = conn.cursor(dictionary=True)
        
        if estado_filtro:
            cursor.execute("SELECT * FROM tareas WHERE estado = %s ORDER BY fecha_creacion DESC", (estado_filtro,))
        else:
            cursor.execute("SELECT * FROM tareas ORDER BY fecha_creacion DESC")
            
        tareas = cursor.fetchall()
        
        # Convertir fechas a formato legible
        for tarea in tareas:
            if tarea['fecha_creacion']:
                if isinstance(tarea['fecha_creacion'], str):
                    tarea['fecha_creacion'] = tarea['fecha_creacion']
                else:
                    tarea['fecha_creacion'] = tarea['fecha_creacion'].strftime('%d/%m/%Y %H:%M')
            
            if tarea['fecha_vencimiento']:
                if isinstance(tarea['fecha_vencimiento'], str):
                    tarea['fecha_vencimiento'] = tarea['fecha_vencimiento']
                else:
                    tarea['fecha_vencimiento'] = tarea['fecha_vencimiento'].strftime('%d/%m/%Y')
        
        cursor.close()
        conn.close()
        return render_template('index.html', tareas=tareas, estado_filtro=estado_filtro)
    
    except mysql.connector.Error as err:
        flash(f'Error al cargar tareas: {err}', 'error')
        return render_template('index.html', tareas=[])

@app.route('/agregar', methods=['POST'])
def agregar():
    titulo = request.form.get('titulo', '').strip()
    descripcion = request.form.get('descripcion', '').strip()
    fecha_vencimiento = request.form.get('fecha_vencimiento') or None
    
    if not titulo:
        flash('El título es obligatorio', 'error')
        return redirect(url_for('index'))
    
    conn = get_db_connection()
    if not conn:
        flash('Error de conexión a la base de datos', 'error')
        return redirect(url_for('index'))
    
    try:
        cursor = conn.cursor()
        cursor.execute(
            "INSERT INTO tareas (titulo, descripcion, fecha_vencimiento) VALUES (%s, %s, %s)",
            (titulo, descripcion, fecha_vencimiento)
        )
        conn.commit()
        cursor.close()
        conn.close()
        flash('✅ Tarea agregada correctamente', 'success')
    except mysql.connector.Error as err:
        flash(f'❌ Error al agregar tarea: {err}', 'error')
    
    return redirect(url_for('index'))

@app.route('/editar/<int:id>')
def editar_form(id):
    conn = get_db_connection()
    if not conn:
        flash('Error de conexión a la base de datos', 'error')
        return redirect(url_for('index'))
    
    try:
        cursor = conn.cursor(dictionary=True)
        cursor.execute("SELECT * FROM tareas WHERE id = %s", (id,))
        tarea = cursor.fetchone()
        
        if not tarea:
            flash('Tarea no encontrada', 'error')
            return redirect(url_for('index'))
        
        # Formatear fecha para el input date
        if tarea['fecha_vencimiento']:
            if isinstance(tarea['fecha_vencimiento'], str):
                tarea['fecha_vencimiento_input'] = tarea['fecha_vencimiento']
            else:
                tarea['fecha_vencimiento_input'] = tarea['fecha_vencimiento'].strftime('%Y-%m-%d')
        else:
            tarea['fecha_vencimiento_input'] = ''
        
        cursor.close()
        conn.close()
        return render_template('editar.html', tarea=tarea)
    
    except mysql.connector.Error as err:
        flash(f'Error al cargar tarea: {err}', 'error')
        return redirect(url_for('index'))

@app.route('/editar', methods=['POST'])
def editar():
    id = request.form.get('id')
    titulo = request.form.get('titulo', '').strip()
    descripcion = request.form.get('descripcion', '').strip()
    estado = request.form.get('estado', 'pendiente')
    fecha_vencimiento = request.form.get('fecha_vencimiento') or None
    
    if not titulo:
        flash('El título es obligatorio', 'error')
        return redirect(url_for('editar_form', id=id))
    
    conn = get_db_connection()
    if not conn:
        flash('Error de conexión a la base de datos', 'error')
        return redirect(url_for('index'))
    
    try:
        cursor = conn.cursor()
        cursor.execute(
            """UPDATE tareas 
            SET titulo=%s, descripcion=%s, estado=%s, fecha_vencimiento=%s 
            WHERE id=%s""",
            (titulo, descripcion, estado, fecha_vencimiento, id)
        )
        conn.commit()
        cursor.close()
        conn.close()
        flash('✅ Tarea actualizada correctamente', 'success')
    except mysql.connector.Error as err:
        flash(f'❌ Error al actualizar tarea: {err}', 'error')
    
    return redirect(url_for('index'))

@app.route('/eliminar/<int:id>')
def eliminar(id):
    conn = get_db_connection()
    if not conn:
        flash('Error de conexión a la base de datos', 'error')
        return redirect(url_for('index'))
    
    try:
        cursor = conn.cursor()
        cursor.execute("DELETE FROM tareas WHERE id = %s", (id,))
        conn.commit()
        cursor.close()
        conn.close()
        flash('✅ Tarea eliminada correctamente', 'success')
    except mysql.connector.Error as err:
        flash(f'❌ Error al eliminar tarea: {err}', 'error')
    
    return redirect(url_for('index'))

@app.route('/cambiar_estado/<int:id>/<estado>')
def cambiar_estado(id, estado):
    if estado not in ['pendiente', 'en_progreso', 'completada']:
        flash('Estado no válido', 'error')
        return redirect(url_for('index'))
    
    conn = get_db_connection()
    if not conn:
        flash('Error de conexión a la base de datos', 'error')
        return redirect(url_for('index'))
    
    try:
        cursor = conn.cursor()
        cursor.execute("UPDATE tareas SET estado = %s WHERE id = %s", (estado, id))
        conn.commit()
        cursor.close()
        conn.close()
        flash('✅ Estado actualizado correctamente', 'success')
    except mysql.connector.Error as err:
        flash(f'❌ Error al cambiar estado: {err}', 'error')
    
    return redirect(url_for('index'))

if __name__ == '__main__':
    print("🌐 Iniciando servidor Flask...")
    print("📊 Asegúrate de que MySQL esté ejecutándose")
    print("🔗 La aplicación estará disponible en: http://localhost:5000")
    app.run(debug=True, host='0.0.0.0', port=5000)