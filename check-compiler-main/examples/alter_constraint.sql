ALTER TABLE products
ADD CONSTRAINT ck_price CHECK (price > 0);