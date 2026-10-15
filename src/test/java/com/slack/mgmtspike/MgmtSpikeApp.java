package com.slack.mgmtspike;

import org.springframework.boot.autoconfigure.EnableAutoConfiguration;
import org.springframework.boot.SpringBootConfiguration;

/** 관리 서버 시험용 최소 앱. 스캔 루트가 com.slack.mgmtspike라 com.slack.labmgmt 설정은 주 컨텍스트에 들어오지 못한다(R2 확인용). */
@SpringBootConfiguration
@EnableAutoConfiguration(excludeName = {
        "org.springframework.boot.autoconfigure.jdbc.DataSourceAutoConfiguration",
        "org.springframework.boot.autoconfigure.jdbc.DataSourceTransactionManagerAutoConfiguration",
        "org.springframework.boot.autoconfigure.jdbc.JdbcTemplateAutoConfiguration",
        "org.springframework.boot.autoconfigure.flyway.FlywayAutoConfiguration",
        "org.springframework.boot.autoconfigure.sql.init.SqlInitializationAutoConfiguration"})
public class MgmtSpikeApp {
}
