-- ============================================================
-- TaskKanban 任务调度看板 - 数据库 Schema
-- 适用于 MySQL 8.0+
-- ============================================================

CREATE DATABASE IF NOT EXISTS taskkanban
    CHARACTER SET utf8mb4
    COLLATE utf8mb4_unicode_ci;

USE taskkanban;

-- 任务表
CREATE TABLE IF NOT EXISTS tasks (
    id             BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
    status         ENUM('pending','claimed','running','done','failed') NOT NULL DEFAULT 'pending',
    base_params    JSON NOT NULL,
    group_override JSON NOT NULL,
    claimed_by     VARCHAR(255) NULL,
    claimed_at     DATETIME NULL,
    started_at     DATETIME NULL,
    finished_at    DATETIME NULL,
    created_at     DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at     DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
    PRIMARY KEY (id),
    KEY idx_tasks_status_created (status, created_at)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

-- 步骤表
CREATE TABLE IF NOT EXISTS steps (
    id         BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
    task_id    BIGINT UNSIGNED NOT NULL,
    step_index INT NOT NULL,
    override   JSON NOT NULL,
    status     ENUM('pending','running','done','failed') NOT NULL DEFAULT 'pending',
    started_at DATETIME NULL,
    finished_at DATETIME NULL,
    PRIMARY KEY (id),
    UNIQUE KEY uk_task_step (task_id, step_index),
    KEY idx_steps_task_status (task_id, status),
    CONSTRAINT fk_steps_task
        FOREIGN KEY (task_id) REFERENCES tasks (id)
        ON DELETE CASCADE
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

-- 步骤执行日志表（幂等核心：同一 task_id + step_index 只允许一条）
CREATE TABLE IF NOT EXISTS step_logs (
    id         BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
    task_id    BIGINT UNSIGNED NOT NULL,
    step_index INT NOT NULL,
    status     ENUM('success','failure') NOT NULL,
    message    TEXT NULL,
    created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
    PRIMARY KEY (id),
    UNIQUE KEY uk_log_task_step (task_id, step_index),
    CONSTRAINT fk_logs_task
        FOREIGN KEY (task_id) REFERENCES tasks (id)
        ON DELETE CASCADE
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;
