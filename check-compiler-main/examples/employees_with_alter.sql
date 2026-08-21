CREATE TABLE employees (
    id INT PRIMARY KEY,
    salary INT CHECK (salary > 0),
    bonus INT,
    CONSTRAINT ck_bonus CHECK (bonus >= 0 AND bonus <= salary),
    name TEXT,
    CHECK (id > 0)
);

ALTER TABLE employees
ADD CONSTRAINT ck_name_len CHECK (length(name) <= 50);
