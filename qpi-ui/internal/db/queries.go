package db

import (
	"errors"
	"qpi/internal/config"
	"qpi/internal/lib"

	"github.com/pocketbase/dbx"
	"github.com/pocketbase/pocketbase/core"
)

var (
	ErrNotFound = errors.New("not found")
)

// OwnRecordsFilter creates a filter for a given user's records
func OwnRecordsFilter(userField string, userId string) func(q *dbx.SelectQuery) error {
	return MapFilter(map[string]any{userField: userId})
}

// MapFilter creates a filter from a map of field-value pairs
func MapFilter(data map[string]any) func(q *dbx.SelectQuery) error {
	exprs := make(dbx.HashExp, len(data))
	for k, v := range data {
		exprs[k] = v
	}

	return func(q *dbx.SelectQuery) error {
		q.AndWhere(exprs)
		return nil
	}
}

// GetUserByToken finds a user by their API token.
// It will return a user only if the token is not expired and active.
func GetUserByToken(app core.App, token string) (*User, error) {
	if token == "" {
		return nil, errors.New("empty token")
	}

	cfg, err := config.GetConfigFromApp(app)
	if err != nil {
		return nil, err
	}

	col := cfg.CollectionAPITokens
	hashedToken := HashToken(token)
	timestamp := lib.GetUtcNow()

	tokenRecord, err := app.FindFirstRecordByFilter(
		col,
		"token = {:token} && (expires_at = '' || expires_at >= {:now})",
		dbx.Params{"token": hashedToken, "now": timestamp},
	)
	if err != nil {
		return nil, err
	}

	userID := tokenRecord.GetString("user")
	if userID == "" {
		return nil, errors.New("token has no associated user")
	}

	userRecord, err := app.FindRecordById("users", userID)
	if err != nil {
		return nil, ErrNotFound
	}
	user := User{}
	err = user.RefreshFromRecord(userRecord)
	if err != nil {
		return nil, err
	}

	return &user, nil
}

// FindMany filters many items from the database and populates the dest slice
func FindMany[T any, PT interface {
	*T
	DbModel
}](app core.App, collectionName string, dest *[]T, filter string,
	sort string,
	limit int,
	offset int,
	params ...dbx.Params) error {
	records, err := app.FindRecordsByFilter(
		collectionName,
		filter,
		sort,
		limit,
		offset,
		params...)
	if err != nil {
		return err
	}

	list := make([]T, 0, len(records))
	for _, record := range records {
		var t T
		var ptr PT = &t
		err := ptr.RefreshFromRecord(record)
		if err != nil {
			return err
		}
		list = append(list, t)
	}
	*dest = list
	return nil
}

// FindOne gets the record of the given id from the database and populates dest
func FindOne(app core.App, collectionName string, id string, dest DbModel, optFilters ...func(q *dbx.SelectQuery) error) error {
	record, err := app.FindRecordById(collectionName, id, optFilters...)
	if err != nil {
		return ErrNotFound
	}

	return dest.RefreshFromRecord(record)
}

// FindAndUpdateOne gets the record with the given id, updates it, and populates dest
func FindAndUpdateOne(app core.App, collectionName string, id string, dest DbModel, updateData map[string]any, optFilters ...func(q *dbx.SelectQuery) error) error {
	record, err := app.FindRecordById(collectionName, id, optFilters...)
	if err != nil {
		return ErrNotFound
	}

	for k, v := range updateData {
		record.Set(k, v)
	}

	err = app.Save(record)
	if err != nil {
		return err
	}

	return dest.RefreshFromRecord(record)
}

// FindAndDeleteOne gets the record with the given id, deletes it, and populates dest with the record's data before deletion
func FindAndDeleteOne(app core.App, collectionName string, id string, dest DbModel, optFilters ...func(q *dbx.SelectQuery) error) error {
	record, err := app.FindRecordById(collectionName, id, optFilters...)
	if err != nil {
		return ErrNotFound
	}

	err = dest.RefreshFromRecord(record)
	if err != nil {
		return err
	}

	return app.Delete(record)
}

// FindOneByFilter finds a single record using a custom filter expression and populates dest
func FindOneByFilter(app core.App, collectionName string, dest DbModel, filter string, params ...dbx.Params) error {
	record, err := app.FindFirstRecordByFilter(collectionName, filter, params...)
	if err != nil {
		return ErrNotFound
	}

	return dest.RefreshFromRecord(record)
}

// AllowedValues returns what a select column accepts, read from the live schema.
// An unknown collection, or a column that is not a select, returns nil.
func AllowedValues(app core.App, collectionName string, fieldName string) []string {
	col, err := app.FindCollectionByNameOrId(collectionName)
	if err != nil {
		return nil
	}
	field, ok := col.Fields.GetByName(fieldName).(*core.SelectField)
	if !ok {
		return nil
	}
	return field.Values
}
