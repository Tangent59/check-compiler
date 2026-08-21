DROP TABLE IF EXISTS employees_native CASCADE;
DROP TABLE IF EXISTS employees_a CASCADE;
DROP TABLE IF EXISTS employees_b CASCADE;

CREATE TABLE employees_native (
    id INT PRIMARY KEY,
    salary INT CHECK (salary > 0),
    bonus INT,
    name TEXT,
    CHECK (id > 0),
    CONSTRAINT ck_bonus CHECK (bonus >= 0 AND bonus <= salary),
    CONSTRAINT ck_name_len CHECK (length(name) <= 50)
);

CREATE TABLE employees_a (
    id INT PRIMARY KEY,
    salary INT,
    bonus INT,
    name TEXT
);

CREATE TABLE employees_b (
    id INT PRIMARY KEY,
    salary INT,
    bonus INT,
    name TEXT
);
