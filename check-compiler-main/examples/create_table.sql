CREATE TABLE products (
    id int primary key,
    price numeric,
    discount numeric,
    CHECK (price > 0),
    CHECK (discount >= 0 AND discount <= price)
);