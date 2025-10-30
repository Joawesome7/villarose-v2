from flask import Flask, render_template, request, jsonify
from flask_sqlalchemy import SQLAlchemy
import gspread
from google.oauth2.service_account import Credentials
from dotenv import load_dotenv
from datetime import datetime, date, timedelta, timezone
import os

# Load .env only in development
if os.getenv("FLASK_ENV") == "development":
    load_dotenv()

app = Flask(__name__)

# Secret key
app.secret_key = os.environ.get("SECRET_KEY") or os.environ.get("FLASK_SECRET_KEY") or "dev-fallback-key-unsafe"

# --- DATABASE ---
database_url = os.environ.get("DATABASE_URL")
# Debug - print what we're actually getting
print(f"🔍 DATABASE_URL exists: {database_url is not None}")
if database_url:
    # Parse to show components without exposing full password
    from urllib.parse import urlparse
    parsed = urlparse(database_url)
    print(f"🔍 Host: {parsed.hostname}")
    print(f"🔍 Port: {parsed.port}")
    print(f"🔍 User: {parsed.username}")
    print(f"🔍 Password starts with: {parsed.password[:5] if parsed.password else 'NONE'}...")
    print(f"🔍 Database: {parsed.path[1:]}")

# Debug logging (remove after fixing)
print(f"🔍 FLASK_ENV: {os.environ.get('FLASK_ENV')}")
print(f"🔍 DATABASE_URL exists: {database_url is not None}")
if database_url:
    # Don't print full URL (has password), just check format
    print(f"🔍 DATABASE_URL starts with: {database_url.split('@')[0] if '@' in database_url else 'invalid'}")

if not database_url:
    if os.environ.get("FLASK_ENV") == "development":
        database_url = "postgresql://localhost/room_booking"
        print("⚠️ Using LOCAL database for development")
    else:
        raise RuntimeError("❌ Missing DATABASE_URL — required in production!")

# Fix postgres:// → postgresql://
if database_url.startswith("postgres://"):
    database_url = database_url.replace("postgres://", "postgresql://", 1)
    print("✅ Fixed postgres:// to postgresql://")

app.config["SQLALCHEMY_DATABASE_URI"] = database_url
app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False
db = SQLAlchemy(app)


# Models
class Room(db.Model):
    __tablename__ = 'rooms'
    
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(100), nullable=False)
    image = db.Column(db.String(500))
    beds = db.Column(db.String(50))
    min_guests = db.Column(db.Integer, nullable=False, default=1)
    max_guests = db.Column(db.Integer, nullable=False)
    price = db.Column(db.Integer, nullable=False)
    available = db.Column(db.Boolean, default=True)
    description = db.Column(db.Text)
    total_units = db.Column(db.Integer, nullable=False, default=1)  # NEW: Total units available
    
    amenities = db.relationship('Amenity', backref='room', lazy=True, cascade='all, delete-orphan')
    gallery_images = db.relationship('GalleryImage', backref='room', lazy=True, cascade='all, delete-orphan')
    bookings = db.relationship('Booking', backref='room', lazy=True)

class Amenity(db.Model):
    __tablename__ = 'amenities'
    
    id = db.Column(db.Integer, primary_key=True)
    room_id = db.Column(db.Integer, db.ForeignKey('rooms.id'), nullable=False)
    name = db.Column(db.String(100), nullable=False)

class GalleryImage(db.Model):
    __tablename__ = 'gallery_images'
    
    id = db.Column(db.Integer, primary_key=True)
    room_id = db.Column(db.Integer, db.ForeignKey('rooms.id'), nullable=False)
    image_url = db.Column(db.String(500), nullable=False)
    order = db.Column(db.Integer, default=0)

class Booking(db.Model):
    __tablename__ = 'bookings'
    
    id = db.Column(db.Integer, primary_key=True)
    room_id = db.Column(db.Integer, db.ForeignKey('rooms.id'), nullable=False)
    check_in = db.Column(db.Date, nullable=False)
    check_out = db.Column(db.Date, nullable=False)
    guests = db.Column(db.Integer, nullable=False)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

# I run this to fix production issue in railway
# with app.app_context():
#     db.create_all()
#     if Room.query.count() == 0:
#         # Add your sample data here (copy from init_db)
#         # ... (your rooms_data loop)
#         db.session.commit()
#         print("✅ Sample data loaded!")

# Helper functions
def get_available_units(room_id, check_in, check_out):
    """Get number of available units for a room in a date range"""
    room = Room.query.get(room_id)
    if not room:
        return 0
    
    # Count how many bookings overlap with this date range
    overlapping_bookings = Booking.query.filter(
        Booking.room_id == room_id,
        Booking.check_out > check_in,
        Booking.check_in < check_out
    ).count()
    
    available = room.total_units - overlapping_bookings
    return max(0, available)

def is_room_available(room_id, check_in, check_out):
    """Check if at least one unit is available for booking"""
    return get_available_units(room_id, check_in, check_out) > 0

def get_booked_dates_by_unit(room_id, start_date, end_date):
    """Get availability info per date for calendar display"""
    room = Room.query.get(room_id)
    if not room:
        return {}
    
    bookings = Booking.query.filter(
        Booking.room_id == room_id,
        Booking.check_out > start_date,
        Booking.check_in < end_date
    ).all()
    
    # Count bookings per date
    date_counts = {}
    for booking in bookings:
        current = booking.check_in
        while current < booking.check_out:
            date_str = current.strftime('%Y-%m-%d')
            date_counts[date_str] = date_counts.get(date_str, 0) + 1
            current += timedelta(days=1)
    
    # Determine availability status per date
    availability = {}
    for date_str, count in date_counts.items():
        available_units = room.total_units - count
        availability[date_str] = {
            'booked_units': count,
            'available_units': available_units,
            'total_units': room.total_units,
            'fully_booked': available_units == 0
        }
    
    return availability

def append_to_google_sheet(data):
    load_dotenv()

    creds_path = os.getenv("GOOGLE_CREDS_PATH")
    sheet_id = os.getenv("GOOGLE_SHEET_ID")

    # Authenticate using the service account file
    creds = Credentials.from_service_account_file(
        creds_path,
        scopes=["https://www.googleapis.com/auth/spreadsheets"]
    )

    client = gspread.authorize(creds)
    sheet = client.open_by_key(sheet_id).sheet1

    # Append a new row with booking details
    sheet.append_row([
        data.get("room_id"),
        data.get("check_in"),
        data.get("guests"),
        data.get("check_out"),
        data.get("created_at")
    ])

# Routes
@app.route('/')
def index():
    today = date.today()
    tomorrow = today + timedelta(days=1)
    return render_template('index.html', today=today.strftime('%Y-%m-%d'), tomorrow=tomorrow.strftime('%Y-%m-%d'))

@app.route('/search')
def search_rooms():
    guests = request.args.get('guests', type=int)
    check_in_str = request.args.get('checkIn')
    check_out_str = request.args.get('checkOut')
    
    query = Room.query.filter_by(available=True)
    
    # Filter by guest count if provided
    if guests:
        query = query.filter(Room.min_guests <= guests, Room.max_guests >= guests)
    
    rooms = query.all()
    
    # Filter by availability if dates provided
    if check_in_str and check_out_str:
        try:
            check_in = datetime.strptime(check_in_str, '%Y-%m-%d').date()
            check_out = datetime.strptime(check_out_str, '%Y-%m-%d').date()
            
            # Add available units info to each room
            available_rooms = []
            for room in rooms:
                available_units = get_available_units(room.id, check_in, check_out)
                if available_units > 0:
                    room.available_units = available_units
                    available_rooms.append(room)
            rooms = available_rooms
        except ValueError:
            # If date parsing fails, show all rooms with full unit count
            for room in rooms:
                room.available_units = room.total_units
    else:
        # No dates specified, show all rooms with full unit count
        for room in rooms:
            room.available_units = room.total_units
    
    return render_template('partials/rooms_grid.html', rooms=rooms, guests=guests, check_in=check_in_str, check_out=check_out_str)

@app.route('/room/<int:room_id>')
def room_detail(room_id):
    room = Room.query.get_or_404(room_id)
    check_in_str = request.args.get('checkIn', date.today().strftime('%Y-%m-%d'))
    check_out_str = request.args.get('checkOut', (date.today() + timedelta(days=1)).strftime('%Y-%m-%d'))
    guests = request.args.get('guests', 1)
    
    # Calculate available units for this date range
    try:
        check_in = datetime.strptime(check_in_str, '%Y-%m-%d').date()
        check_out = datetime.strptime(check_out_str, '%Y-%m-%d').date()
        room.available_units = get_available_units(room.id, check_in, check_out)
    except ValueError:
        room.available_units = room.total_units
    
    return render_template('partials/room_detail.html', room=room, check_in=check_in_str, check_out=check_out_str, guests=guests)

@app.route('/api/room/<int:room_id>/availability')
def room_availability(room_id):
    """API endpoint to get availability data for calendar"""
    room = Room.query.get_or_404(room_id)
    
    month = request.args.get('month', type=int, default=date.today().month)
    year = request.args.get('year', type=int, default=date.today().year)
    
    # Calculate date range (current month + next 2 months)
    start_date = date(year, month, 1)
    if month + 2 > 12:
        end_date = date(year + 1, (month + 2) % 12 or 12, 1)
    else:
        end_date = date(year, month + 3, 1) if month + 3 <= 12 else date(year + 1, 1, 1)
    
    availability = get_booked_dates_by_unit(room_id, start_date, end_date)
    
    return jsonify({
        'room_id': room_id,
        'total_units': room.total_units,
        'availability': availability,
        'start_date': start_date.strftime('%Y-%m-%d'),
        'end_date': end_date.strftime('%Y-%m-%d')
    })

@app.route('/calendar/<int:room_id>')
def calendar_view(room_id):
    """Render the calendar component"""
    room = Room.query.get_or_404(room_id)
    check_in = request.args.get('checkIn', date.today().strftime('%Y-%m-%d'))
    check_out = request.args.get('checkOut', (date.today() + timedelta(days=1)).strftime('%Y-%m-%d'))
    guests = request.args.get('guests', 1)
    
    return render_template('partials/calendar.html', 
                         room=room, 
                         check_in=check_in, 
                         check_out=check_out, 
                         guests=guests)

# @app.route('/booking/confirm', methods=['POST'])
# def confirm_booking():
#     try:
#         # Get form data
#         room_id = request.form.get('room_id', type=int)
#         check_in_str = request.form.get('check_in')
#         check_out_str = request.form.get('check_out')
#         guests = request.form.get('guests', type=int)
        
#         # Validate required fields
#         if not all([room_id, check_in_str, check_out_str, guests]):
#             missing = []
#             if not room_id: missing.append('room_id')
#             if not check_in_str: missing.append('check_in')
#             if not check_out_str: missing.append('check_out')
#             if not guests: missing.append('guests')
#             return render_template('partials/booking_error.html', 
#                                  error=f'Missing required fields: {", ".join(missing)}'), 400
        
#         # Parse dates
#         check_in = datetime.strptime(check_in_str, '%Y-%m-%d').date()
#         check_out = datetime.strptime(check_out_str, '%Y-%m-%d').date()
        
#         # Validate dates
#         if check_in >= check_out:
#             return render_template('partials/booking_error.html', 
#                                  error='Check-out date must be after check-in date'), 400
        
#         if check_in < date.today():
#             return render_template('partials/booking_error.html', 
#                                  error='Check-in date cannot be in the past'), 400
        
#         # Get room
#         # room = Room.query.get_or_404(room_id)
#         room = db.session.get(Room, room_id)

        
#         # Validate guest count
#         if guests < room.min_guests or guests > room.max_guests:
#             return render_template('partials/booking_error.html', 
#                                  error=f'Guest count must be between {room.min_guests} and {room.max_guests}'), 400
        
#         # Check if at least one unit is available
#         available_units = get_available_units(room_id, check_in, check_out)
#         if available_units <= 0:
#             return render_template('partials/booking_error.html', 
#                                  error=f'No units available for {room.name} on selected dates. All {room.total_units} unit(s) are booked.'), 400
        
#         # Create booking
#         booking = Booking(
#             room_id=room_id,
#             check_in=check_in,
#             check_out=check_out,
#             guests=guests
#         )
        
#         db.session.add(booking)
#         db.session.commit()

#         append_to_google_sheet({
#             "room_id": room_id,
#             "check_in": check_in.strftime('%Y-%m-%d'),
#             "check_out": check_out.strftime('%Y-%m-%d'),
#             "guests": guests,
#             "created_at": datetime.utcnow().isoformat()
#         })

        
#         print(f"✅ Booking created successfully: ID {booking.id}, Room: {room.name}, Units remaining: {available_units - 1}/{room.total_units}")
        
#         nights = (check_out - check_in).days
#         total_price = room.price * nights
        
#         return render_template('partials/booking_confirmation.html', 
#                              room=room, 
#                              booking=booking, 
#                              total_price=total_price, 
#                              nights=nights,
#                              remaining_units=available_units - 1)
    
#     except ValueError as e:
#         return render_template('partials/booking_error.html', 
#                              error='Invalid date format'), 400
#     except Exception as e:
#         print(f"❌ Booking error: {str(e)}")
#         import traceback
#         traceback.print_exc()
#         return render_template('partials/booking_error.html', 
#                              error='An error occurred while processing your booking'), 500

@app.route('/booking/confirm', methods=['POST'])
def confirm_booking():
    try:
        # Get form data
        room_id = request.form.get('room_id', type=int)
        check_in_str = request.form.get('check_in')
        check_out_str = request.form.get('check_out')
        guests = request.form.get('guests', type=int)

        # Validate required fields
        if not all([room_id, check_in_str, check_out_str, guests]):
            missing = []
            if not room_id: missing.append('room_id')
            if not check_in_str: missing.append('check_in')
            if not check_out_str: missing.append('check_out')
            if not guests: missing.append('guests')
            return render_template('partials/booking_error.html', 
                                   error=f'Missing required fields: {", ".join(missing)}'), 400

        # Parse dates
        check_in = datetime.strptime(check_in_str, '%Y-%m-%d').date()
        check_out = datetime.strptime(check_out_str, '%Y-%m-%d').date()

        # Validate dates
        if check_in >= check_out:
            return render_template('partials/booking_error.html', 
                                   error='Check-out date must be after check-in date'), 400

        if check_in < date.today():
            return render_template('partials/booking_error.html', 
                                   error='Check-in date cannot be in the past'), 400

        # Get room
        room = db.session.get(Room, room_id)
        if not room:
            return render_template('partials/booking_error.html',
                                   error='Room not found.'), 404

        # Validate guest count
        if guests < room.min_guests or guests > room.max_guests:
            return render_template('partials/booking_error.html', 
                                   error=f'Guest count must be between {room.min_guests} and {room.max_guests}'), 400

        # Check available units
        available_units = get_available_units(room_id, check_in, check_out)
        if available_units <= 0:
            return render_template('partials/booking_error.html', 
                                   error=f'No units available for {room.name} on selected dates.'), 400

        # Create booking
        booking = Booking(
            room_id=room_id,
            check_in=check_in,
            check_out=check_out,
            guests=guests
        )
        db.session.add(booking)
        db.session.commit()

        # Try appending to Google Sheets (but don’t break if it fails)
        try:
            append_to_google_sheet({
                "room_id": room_id,
                "check_in": check_in.strftime('%Y-%m-%d'),
                "check_out": check_out.strftime('%Y-%m-%d'),
                "guests": guests,
                "created_at": datetime.now(timezone.utc).isoformat()
            })
            print("✅ Google Sheets updated successfully.")
        except Exception as e:
            print(f"⚠️ Google Sheets append failed: {e}")

        # Continue showing confirmation regardless
        nights = (check_out - check_in).days
        total_price = room.price * nights

        print(f"✅ Booking created successfully: ID {booking.id}, Room: {room.name}, Units remaining: {available_units - 1}/{room.total_units}")

        return render_template('partials/booking_confirmation.html',
                               room=room,
                               booking=booking,
                               total_price=total_price,
                               nights=nights,
                               remaining_units=available_units - 1)

    except ValueError:
        return render_template('partials/booking_error.html',
                               error='Invalid date format'), 400
    except Exception as e:
        print(f"❌ Booking error: {e}")
        import traceback
        traceback.print_exc()
        return render_template('partials/booking_error.html',
                               error='An error occurred while processing your booking'), 500


# Initialize database
def init_db():
    with app.app_context():
        db.create_all()
        
        # Check if data already exists
        if Room.query.count() == 0:
            # Sample data with unit counts
            rooms_data = [
                {
                    'name': 'Bungalow',
                    'image': 'https://placehold.co/400x300/667eea/white?text=Bungalow',
                    'beds': '1 Bedroom',
                    'min_guests': 1,
                    'max_guests': 2,
                    'price': 10000,
                    'total_units': 1,  # 1 unit
                    'description': 'Experience luxury in our spacious Deluxe King Room featuring premium bedding, a 55-inch smart TV, and a private balcony with city views. Perfect for couples seeking comfort and style.',
                    'amenities': ['Service Kitchen / Laundry', 'Parking', 'Sofa Bed', 'Refrigerator', 'AC', 'Balcony', 'Dining Kitchen', 'Water Heater'],
                    'gallery': [
                        'https://placehold.co/600x400/667eea/white?text=Deluxe+King+View+1',
                        'https://placehold.co/600x400/764ba2/white?text=Deluxe+King+Bathroom',
                        'https://placehold.co/600x400/f093fb/white?text=Deluxe+King+Balcony'
                    ]
                },
                {
                    'name': 'Deluxe Room',
                    'image': 'https://placehold.co/400x300/764ba2/white?text=Deluxe',
                    'beds': 'Queen Size Bed /W Pull Out Bed',
                    'min_guests': 2,
                    'max_guests': 3,
                    'price': 3500,
                    'total_units': 2,  # 2 units
                    'description': 'Our Deluxe room offers separate living and sleeping areas, perfect for business travelers or small families. Features include a work desk, premium entertainment system, and access to executive lounge.',
                    'amenities': ['Ocean View / Nature View', 'Shared Toilet Bath Room', '2nd Floor Level With Plated Breakfast'],
                    'gallery': [
                        'https://placehold.co/600x400/764ba2/white?text=Executive+Suite+Living',
                        'https://placehold.co/600x400/4facfe/white?text=Executive+Suite+Bedroom',
                        'https://placehold.co/600x400/43e97b/white?text=Executive+Suite+Bathroom'
                    ]
                },
                {
                    'name': 'Suite Room',
                    'image': 'https://placehold.co/400x300/f093fb/white?text=Suite',
                    'beds': 'King Size Bed',
                    'min_guests': 1,
                    'max_guests': 2,
                    'price': 4500,
                    'total_units': 1,  # 1 unit
                    'description': 'Comfortable and affordable, our Standard Double Room is ideal for families or groups. Features two comfortable double beds, modern amenities, and a cozy atmosphere for a restful stay.',
                    'amenities': ['Ocean View', 'Pool View', 'Toilet and Bath W/ Bath Thub', '2nd Floor Level With Plated Breakfast'],
                    'gallery': [
                        'https://placehold.co/600x400/f093fb/white?text=Standard+Double+Room',
                        'https://placehold.co/600x400/fbbf24/white?text=Standard+Double+Beds',
                        'https://placehold.co/600x400/4facfe/white?text=Standard+Double+Bathroom'
                    ]
                },
                {
                    'name': 'Executive',
                    'image': 'https://placehold.co/400x300/4facfe/white?text=Executive',
                    'beds': 'Queen Size Bed W/ Pull Out Bed',
                    'min_guests': 2,
                    'max_guests': 3,
                    'price': 4000,
                    'total_units': 1,  # 1 unit
                    'description': 'Wake up to breathtaking ocean views in our Premium Ocean View room. Features a king bed with luxury linens, floor-to-ceiling windows, and a private terrace overlooking the sea.',
                    'amenities': ['Ocean View', 'Pool View', 'Toilet & Bath', 'Ground Floor Level With Plated Breakfast'],
                    'gallery': [
                        'https://placehold.co/600x400/4facfe/white?text=Ocean+View+Room',
                        'https://placehold.co/600x400/43e97b/white?text=Ocean+View+Terrace',
                        'https://placehold.co/600x400/fbbf24/white?text=Ocean+View+Bathroom'
                    ]
                },
                {
                    'name': 'Family Room',
                    'image': 'https://placehold.co/400x300/43e97b/white?text=Family+Room',
                    'beds': '2 Bunk Beds',
                    'min_guests': 2,
                    'max_guests': 4,
                    'price': 4000,
                    'total_units': 1,  # 1 unit
                    'description': 'Designed for families, our spacious Family Room features two queen beds, ample space for children to play, and family-friendly amenities. Includes access to our kids club and family activities.',
                    'amenities': ['Shared Toilet & Bath', 'Plated Breakfast'],
                    'gallery': [
                        'https://placehold.co/600x400/43e97b/white?text=Family+Room+View',
                        'https://placehold.co/600x400/fbbf24/white?text=Family+Room+Beds',
                        'https://placehold.co/600x400/667eea/white?text=Family+Room+Bathroom'
                    ]
                },
                {
                    'name': 'Deluxe Room 2',
                    'image': 'https://placehold.co/400x300/fbbf24/white?text=Deluxe+02+Room',
                    'beds': '1 Queen Bed & Twin Size Bed',
                    'min_guests': 2,
                    'max_guests': 3,
                    'price': 3500,
                    'total_units': 2,  # 2 units
                    'description': 'Perfect for business travelers, our Business Studio combines work and relaxation with a comfortable queen bed, ergonomic workspace, high-speed internet, and business center access.',
                    'amenities': ['Shared Toilet & Bath', 'Plated Breakfast'],
                    'gallery': [
                        'https://placehold.co/600x400/fbbf24/white?text=Business+Studio+Room',
                        'https://placehold.co/600x400/667eea/white?text=Business+Studio+Desk',
                        'https://placehold.co/600x400/764ba2/white?text=Business+Studio+Bathroom'
                    ]
                }
            ]
            
            for room_data in rooms_data:
                room = Room(
                    name=room_data['name'],
                    image=room_data['image'],
                    beds=room_data['beds'],
                    min_guests=room_data['min_guests'],
                    max_guests=room_data['max_guests'],
                    price=room_data['price'],
                    total_units=room_data['total_units'],
                    description=room_data['description']
                )
                db.session.add(room)
                db.session.flush()
                
                for amenity_name in room_data['amenities']:
                    amenity = Amenity(room_id=room.id, name=amenity_name)
                    db.session.add(amenity)
                
                for idx, img_url in enumerate(room_data['gallery']):
                    gallery_img = GalleryImage(room_id=room.id, image_url=img_url, order=idx)
                    db.session.add(gallery_img)
            
            db.session.commit()
            print("✅ Database initialized with sample data!")
            print("📊 Room units: Deluxe Room (2), Deluxe Room 2 (2), Others (1 each)")

def initialize_sample_data():
    with app.app_context():
        db.create_all()
        if Room.query.count() == 0:
            print("🌱 Loading sample room data...")
            # Paste your rooms_data list here (from init_db)
            rooms_data = [ ... ]  # <-- your full list

            for room_data in rooms_data:
                room = Room(
                    name=room_data['name'].strip(),
                    image=room_data['image'].strip(),
                    beds=room_data['beds'],
                    min_guests=room_data['min_guests'],
                    max_guests=room_data['max_guests'],
                    price=room_data['price'],
                    total_units=room_data['total_units'],
                    description=room_data['description']
                )
                db.session.add(room)
                db.session.flush()

                for amenity in room_data['amenities']:
                    db.session.add(Amenity(room_id=room.id, name=amenity.strip()))

                for i, img in enumerate(room_data['gallery']):
                    db.session.add(GalleryImage(
                        room_id=room.id,
                        image_url=img.strip(),
                        order=i
                    ))

            db.session.commit()
            print("✅ Sample data loaded!")
        else:
            print("✅ Data already exists. Skipping initialization.")

# Run initialization only in production (not local dev)
if os.environ.get("FLASK_ENV") != "development":
    initialize_sample_data()

if __name__ == '__main__':
    init_db()
    app.run(debug=True)