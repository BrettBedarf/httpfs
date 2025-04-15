package util

import (
	"os"
	"time"

	"github.com/rs/zerolog"
	"github.com/rs/zerolog/log"
)

// LogLevel represents available log levels
type LogLevel string

const (
	// Log levels
	DebugLevel LogLevel = "debug"
	InfoLevel  LogLevel = "info"
	WarnLevel  LogLevel = "warn"
	ErrorLevel LogLevel = "error"
)

// InitializeLogger sets up the global logger with the specified configuration
func InitializeLogger(level LogLevel) {
	// Set time format to ISO8601
	zerolog.TimeFieldFormat = time.RFC3339

	// Set global log level based on configuration
	switch level {
	case DebugLevel:
		zerolog.SetGlobalLevel(zerolog.DebugLevel)
	case InfoLevel:
		zerolog.SetGlobalLevel(zerolog.InfoLevel)
	case WarnLevel:
		zerolog.SetGlobalLevel(zerolog.WarnLevel)
	case ErrorLevel:
		zerolog.SetGlobalLevel(zerolog.ErrorLevel)
	default:
		zerolog.SetGlobalLevel(zerolog.InfoLevel)
	}

	// Create a console writer with nice formatting for terminal output
	output := zerolog.ConsoleWriter{Out: os.Stdout, TimeFormat: time.RFC3339}

	// Set global logger
	log.Logger = zerolog.New(output).With().Timestamp().Caller().Logger()

	log.Info().Msg("Logger initialized")
}

// GetLogger returns a configured logger for a specific component
func GetLogger(component string) zerolog.Logger {
	return log.With().Str("component", component).Logger()
}
