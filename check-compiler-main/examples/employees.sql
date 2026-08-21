CREATE TABLE employees (
    id INT PRIMARY KEY,
    salary INT CHECK (salary > 0),
    bonus INT,
    name TEXT,
    misc TEXT,
    CHECK (id > 0),
    CONSTRAINT ck_bonus CHECK (bonus >= 0 AND bonus <= salary),
    CONSTRAINT ck_name_len CHECK (length(name) <= 50)
);
