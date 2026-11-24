CREATE TABLE IF NOT EXISTS users (
    id INTEGER PRIMARY KEY,
    username TEXT NOT NULL UNIQUE,
    name TEXT NOT NULL,
    password TEXT NOT NULL,
    role TEXT NOT NULL CHECK(role IN ('customer','staff','admin'))
);
CREATE TABLE IF NOT EXISTS hotels (
    id INTEGER PRIMARY KEY,
    name TEXT NOT NULL,
    city TEXT NOT NULL,
    address TEXT NOT NULL,
    photo TEXT NOT NULL DEFAULT 'hotel-1.jpg',
    closed INTEGER NOT NULL DEFAULT 0 CHECK(closed IN (0,1))
);
CREATE INDEX IF NOT EXISTS hotels_city ON hotels(city);
CREATE TABLE IF NOT EXISTS grants (
    user_id INTEGER REFERENCES users(id),
    hotel_id INTEGER REFERENCES hotels(id),
    PRIMARY KEY(user_id,hotel_id)
);
CREATE TABLE IF NOT EXISTS room_types (
    id INTEGER PRIMARY KEY,
    hotel_id INTEGER NOT NULL REFERENCES hotels(id),
    name TEXT NOT NULL,
    max_guests INTEGER NOT NULL CHECK(max_guests BETWEEN 1 AND 12),
    UNIQUE(hotel_id,name)
);
CREATE TABLE IF NOT EXISTS rooms (
    id INTEGER PRIMARY KEY,
    hotel_id INTEGER NOT NULL REFERENCES hotels(id),
    room_type INTEGER NOT NULL REFERENCES room_types(id),
    number TEXT NOT NULL,
    state TEXT NOT NULL DEFAULT '空闲' CHECK(state IN ('空闲','已入住','待清洁','维修中')),
    UNIQUE(hotel_id,number)
);
CREATE INDEX IF NOT EXISTS rooms_type ON rooms(room_type,state);
CREATE TABLE IF NOT EXISTS inventory (
    room_type INTEGER REFERENCES room_types(id),
    night TEXT NOT NULL,
    total INTEGER NOT NULL CHECK(total >= 0),
    occupied INTEGER NOT NULL DEFAULT 0 CHECK(occupied BETWEEN 0 AND total),
    price INTEGER NOT NULL CHECK(price >= 0),
    stopped INTEGER NOT NULL DEFAULT 0 CHECK(stopped IN (0,1)),
    PRIMARY KEY(room_type,night)
);
CREATE TABLE IF NOT EXISTS bookings (
    id INTEGER PRIMARY KEY,
    customer INTEGER NOT NULL REFERENCES users(id),
    hotel_id INTEGER NOT NULL REFERENCES hotels(id),
    room_type INTEGER NOT NULL REFERENCES room_types(id),
    arrival TEXT NOT NULL,
    departure TEXT NOT NULL CHECK(departure > arrival),
    guest TEXT NOT NULL,
    guests INTEGER NOT NULL CHECK(guests BETWEEN 1 AND 12),
    total INTEGER NOT NULL CHECK(total >= 0),
    state TEXT NOT NULL CHECK(state IN ('待确认','已确认','已入住','已完成','已取消')),
    version INTEGER NOT NULL CHECK(version > 0),
    created INTEGER NOT NULL,
    waiting_since INTEGER NOT NULL,
    cancel_reason TEXT,
    cancel_fee INTEGER CHECK(cancel_fee >= 0)
);
CREATE INDEX IF NOT EXISTS book_customer ON bookings(customer,id DESC);
CREATE INDEX IF NOT EXISTS book_itinerary ON bookings(customer,room_type,arrival,departure);
CREATE INDEX IF NOT EXISTS book_created ON bookings(created,hotel_id);
CREATE INDEX IF NOT EXISTS book_hotel ON bookings(hotel_id,state);
CREATE TABLE IF NOT EXISTS revisions (
    booking_id INTEGER REFERENCES bookings(id),
    version INTEGER,
    room_type INTEGER NOT NULL REFERENCES room_types(id),
    arrival TEXT NOT NULL,
    departure TEXT NOT NULL,
    guest TEXT NOT NULL,
    guests INTEGER NOT NULL,
    total INTEGER NOT NULL,
    changed INTEGER NOT NULL,
    PRIMARY KEY(booking_id,version)
);
CREATE TABLE IF NOT EXISTS nightly_prices (
    booking_id INTEGER,
    version INTEGER,
    night TEXT,
    price INTEGER NOT NULL CHECK(price >= 0),
    PRIMARY KEY(booking_id,version,night),
    FOREIGN KEY(booking_id,version) REFERENCES revisions(booking_id,version)
);
CREATE TABLE IF NOT EXISTS allocations (
    booking_id INTEGER REFERENCES bookings(id),
    room_type INTEGER,
    night TEXT,
    PRIMARY KEY(booking_id,room_type,night),
    FOREIGN KEY(room_type,night) REFERENCES inventory(room_type,night)
);
CREATE INDEX IF NOT EXISTS allocation_day ON allocations(room_type,night);
CREATE TABLE IF NOT EXISTS requests (
    customer INTEGER REFERENCES users(id),
    request_key TEXT,
    fingerprint TEXT NOT NULL,
    state TEXT NOT NULL CHECK(state IN ('OPEN','AWAITING_CONSENT','SUCCEEDED','DECLINED')),
    challenge TEXT,
    matched_booking INTEGER REFERENCES bookings(id),
    booking_id INTEGER REFERENCES bookings(id),
    PRIMARY KEY(customer,request_key)
);
CREATE TABLE IF NOT EXISTS timeout_jobs (
    booking_id INTEGER,
    version INTEGER,
    due INTEGER NOT NULL,
    PRIMARY KEY(booking_id,version),
    FOREIGN KEY(booking_id,version) REFERENCES revisions(booking_id,version)
);
CREATE INDEX IF NOT EXISTS timeout_due ON timeout_jobs(due);
CREATE TABLE IF NOT EXISTS assignments (
    id INTEGER PRIMARY KEY,
    booking_id INTEGER NOT NULL REFERENCES bookings(id),
    room_id INTEGER NOT NULL REFERENCES rooms(id),
    started INTEGER NOT NULL,
    ended INTEGER CHECK(ended >= started)
);
CREATE UNIQUE INDEX IF NOT EXISTS assignment_booking ON assignments(booking_id) WHERE ended IS NULL;
CREATE UNIQUE INDEX IF NOT EXISTS assignment_room ON assignments(room_id) WHERE ended IS NULL;
CREATE TABLE IF NOT EXISTS repairs (
    id INTEGER PRIMARY KEY,
    room_id INTEGER NOT NULL REFERENCES rooms(id),
    room_type INTEGER NOT NULL REFERENCES room_types(id),
    first_night TEXT NOT NULL,
    last_night TEXT NOT NULL CHECK(last_night >= first_night),
    reason TEXT NOT NULL,
    ended INTEGER
);
CREATE UNIQUE INDEX IF NOT EXISTS repair_active ON repairs(room_id) WHERE ended IS NULL;
CREATE INDEX IF NOT EXISTS repair_type ON repairs(room_type,first_night,last_night) WHERE ended IS NULL;
CREATE TABLE IF NOT EXISTS coordination (
    id INTEGER PRIMARY KEY,
    booking_id INTEGER NOT NULL REFERENCES bookings(id),
    reason TEXT NOT NULL,
    opened INTEGER NOT NULL,
    resolution TEXT
);
CREATE UNIQUE INDEX IF NOT EXISTS coordination_open
    ON coordination(booking_id,reason) WHERE resolution IS NULL;
CREATE TABLE IF NOT EXISTS audit (
    operation_id TEXT PRIMARY KEY,
    occurred INTEGER NOT NULL,
    actor TEXT NOT NULL,
    action TEXT NOT NULL,
    target TEXT NOT NULL,
    outcome TEXT NOT NULL,
    detail TEXT NOT NULL,
    before_json TEXT NOT NULL,
    after_json TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS audit_recent ON audit(occurred DESC);
